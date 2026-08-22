from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET

# Configure SSL context
try:
  import certifi

  ssl_context = ssl.create_default_context(cafile=certifi.where())
except ImportError:
  ssl_context = ssl.create_default_context()

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        ' (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'identity',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}

MAX_AGE_HOURS = 24
INITIAL_VISIBLE_COUNT = 4


def clean_text(raw_html: str) -> str:
  if not raw_html:
    return ''
  clean = re.sub(r'<.*?>', '', raw_html)
  return html.unescape(clean).strip()


def sanitize_xml(xml_content: str) -> str:
  """Fixes unescaped ampersands and invalid control characters that break XML parsers."""
  if not xml_content:
    return ''
  cleaned = re.sub(
      r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', xml_content
  )
  cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', cleaned)
  return cleaned


def parse_universal_date(date_str: str) -> datetime | None:
  if not date_str:
    return None
  cleaned = date_str.strip()

  # 1. RSS RFC-822
  try:
    dt = parsedate_to_datetime(cleaned)
    if dt.tzinfo is None:
      return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
  except Exception:
    pass

  # 2. ISO-8601 / Atom
  try:
    iso_clean = cleaned.replace('Z', '+00:00')
    dt = datetime.fromisoformat(iso_clean)
    if dt.tzinfo is None:
      return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
  except Exception:
    pass

  return None


def scrape_fresh_articles(
    feed_tuple: tuple[str, str, str], max_age_hours=MAX_AGE_HOURS
) -> list[dict]:
  source_name, category, feed_url = feed_tuple
  articles = []
  cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

  try:
    req = urllib.request.Request(feed_url, headers=HEADERS)
    with urllib.request.urlopen(
        req, context=ssl_context, timeout=10
    ) as response:
      raw_bytes = response.read()

    raw_text = raw_bytes.decode('utf-8', errors='replace')
    clean_xml_text = sanitize_xml(raw_text)
    root = ET.fromstring(clean_xml_text)

    # RSS 2.0
    channel = root.find('channel')
    if channel is not None:
      for item in channel.findall('item'):
        raw_date = item.findtext('pubDate', default='').strip()
        pub_dt = parse_universal_date(raw_date)

        if pub_dt and pub_dt < cutoff_time:
          continue

        articles.append({
            'source': source_name,
            'category': category,
            'title': clean_text(item.findtext('title', default='No Title')),
            'link': item.findtext('link', default='').strip(),
            'date_obj': pub_dt or datetime.min.replace(tzinfo=timezone.utc),
            'date_str': (
                pub_dt.strftime('%b %d • %I:%M %p UTC') if pub_dt else 'Recent'
            ),
            'desc': (
                clean_text(item.findtext('description', default=''))[:180]
                + '...'
            ),
        })

    # Atom Feed
    elif 'feed' in root.tag.lower():
      ns = {'atom': root.tag.split('}')[0].strip('{')}
      for entry in root.findall('atom:entry', ns):
        raw_date = entry.findtext(
            'atom:published',
            default=entry.findtext('atom:updated', default='', namespaces=ns),
            namespaces=ns,
        ).strip()
        pub_dt = parse_universal_date(raw_date)

        if pub_dt and pub_dt < cutoff_time:
          continue

        link_elem = entry.find('atom:link', ns)
        link = (
            link_elem.attrib.get('href', '').strip()
            if link_elem is not None
            else ''
        )

        articles.append({
            'source': source_name,
            'category': category,
            'title': clean_text(
                entry.findtext('atom:title', default='No Title', namespaces=ns)
            ),
            'link': link,
            'date_obj': pub_dt or datetime.min.replace(tzinfo=timezone.utc),
            'date_str': (
                pub_dt.strftime('%b %d • %I:%M %p UTC') if pub_dt else 'Recent'
            ),
            'desc': (
                clean_text(
                    entry.findtext(
                        'atom:summary',
                        default=entry.findtext(
                            'atom:content', default='', namespaces=ns
                        ),
                        namespaces=ns,
                    )
                )[:180]
                + '...'
            ),
        })

  except Exception as e:
    print(f'Error fetching {source_name}: {e}')

  return articles


def build_card_html(article: dict, is_hidden: bool) -> str:
  badge_class = f"badge-{article.get('category', 'markets')}"
  hidden_class = ' card-extra' if is_hidden else ''
  hidden_style = ' style="display: none;"' if is_hidden else ''

  return f"""
  <article class="news-card{hidden_class}" data-category="{article.get('category', 'markets')}"{hidden_style}>
    <div class="card-meta">
      <div class="card-tags">
        <span class="badge {badge_class}">{article['category'].upper()}</span>
        <span class="source-tag">{article['source']}</span>
      </div>
      <time class="card-time">{article['date_str']}</time>
    </div>
    <h3 class="card-title">
      <a href="{article['link']}" target="_blank" rel="noopener noreferrer">{article['title']}</a>
    </h3>
    <p class="card-desc">{article['desc']}</p>
  </article>
  """


def generate_html_report(all_articles: list[dict], filename='index.html'):
  all_articles.sort(key=lambda x: x['date_obj'], reverse=True)

  sections_config = [
      (
          'economy',
          '🏛️ Economy & Macro Policy',
          'badge-economy',
          'var(--accent-economy)',
      ),
      (
          'markets',
          '📈 Markets & Equities',
          'badge-markets',
          'var(--accent-markets)',
      ),
      (
          'metals',
          '🪙 Precious Metals & Gold',
          'badge-metals',
          'var(--accent-metals)',
      ),
      (
          'crypto',
          '⚡ Crypto & Digital Assets',
          'badge-crypto',
          'var(--accent-crypto)',
      ),
  ]

  sections_html = ''
  for cat_key, cat_title, badge_cls, accent_col in sections_config:
    items = [a for a in all_articles if a.get('category') == cat_key]

    if not items:
      cards_markup = (
          '<p class="empty-state">No stories reported in the last 24 hours.</p>'
      )
      expand_button_markup = ''
    else:
      cards_markup = ''
      for idx, item in enumerate(items):
        is_extra = idx >= INITIAL_VISIBLE_COUNT
        cards_markup += build_card_html(item, is_hidden=is_extra)

      remaining = len(items) - INITIAL_VISIBLE_COUNT
      if remaining > 0:
        expand_button_markup = f"""
        <div class="expand-footer">
          <button class="expand-btn" onclick="toggleSection('{cat_key}')" id="btn-{cat_key}">
            <span>Show {remaining} More Headlines</span>
            <svg class="chevron-icon" viewBox="0 0 20 20" fill="currentColor">
              <path fill-rule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clip-rule="evenodd" />
            </svg>
          </button>
        </div>
        """
      else:
        expand_button_markup = ''

    sections_html += f"""
    <section class="collapsible-section" id="sec-{cat_key}">
      <div class="section-banner">
        <div class="banner-left">
          <span class="accent-bar" style="background: {accent_col};"></span>
          <h2>{cat_title}</h2>
        </div>
        <span class="section-counter">{len(items)} Stories</span>
      </div>
      <div class="cards-grid" id="grid-{cat_key}">
        {cards_markup}
      </div>
      {expand_button_markup}
    </section>
    """

  full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Market News Terminal</title>
  <style>
    :root {{
      --bg-body: #090d16;
      --bg-surface: #111827;
      --bg-surface-hover: #162032;
      --border-color: #1e293b;
      --border-subtle: rgba(255, 255, 255, 0.07);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-economy: #34d399;
      --accent-markets: #38bdf8;
      --accent-crypto: #c084fc;
      --accent-metals: #eab308;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg-body);
      color: var(--text-main);
      padding: 24px 20px;
      line-height: 1.5;
    }}

    header {{
      max-width: 1200px;
      margin: 0 auto 32px auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border-color);
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    h1 {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.4px;
    }}

    .live-indicator {{
      width: 9px;
      height: 9px;
      background: var(--accent-economy);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--accent-economy);
    }}

    .search-bar {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 14px;
      width: 320px;
      outline: none;
      transition: border-color 0.2s;
    }}

    .search-bar:focus {{
      border-color: var(--accent-markets);
    }}

    .main-feed-container {{
      max-width: 1200px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 28px;
    }}

    .collapsible-section {{
      background: rgba(17, 24, 39, 0.4);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      overflow: hidden;
    }}

    .section-banner {{
      background: var(--bg-surface);
      padding: 14px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}

    .banner-left {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    .accent-bar {{
      width: 4px;
      height: 18px;
      border-radius: 2px;
    }}

    .section-banner h2 {{
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.2px;
    }}

    .section-counter {{
      font-size: 11px;
      color: var(--text-muted);
      background: rgba(255, 255, 255, 0.05);
      padding: 3px 9px;
      border-radius: 12px;
      font-weight: 500;
    }}

    /* 2x2 grid inside each collapsible section */
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 14px;
      padding: 18px;
    }}

    .news-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: transform 0.15s ease, background 0.15s ease, border-color 0.15s ease;
    }}

    .news-card:hover {{
      background: var(--bg-surface-hover);
      border-color: rgba(255, 255, 255, 0.15);
      transform: translateY(-1px);
    }}

    .card-meta {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}

    .card-tags {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .badge {{
      font-size: 9px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
      letter-spacing: 0.5px;
    }}

    .badge-economy {{ background: rgba(52, 211, 153, 0.15); color: var(--accent-economy); }}
    .badge-markets {{ background: rgba(56, 189, 248, 0.15); color: var(--accent-markets); }}
    .badge-crypto  {{ background: rgba(192, 132, 252, 0.15); color: var(--accent-crypto); }}
    .badge-metals  {{ background: rgba(234, 179, 8, 0.15); color: var(--accent-metals); }}

    .source-tag {{
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 500;
    }}

    .card-time {{
      font-size: 11px;
      color: var(--text-muted);
    }}

    .card-title {{
      font-size: 14px;
      line-height: 1.4;
      margin-bottom: 8px;
      font-weight: 600;
    }}

    .card-title a {{
      color: var(--text-main);
      text-decoration: none;
    }}

    .card-title a:hover {{
      color: var(--accent-markets);
    }}

    .card-desc {{
      font-size: 12px;
      line-height: 1.45;
      color: var(--text-muted);
    }}

    .expand-footer {{
      border-top: 1px solid var(--border-subtle);
      padding: 10px 18px;
      text-align: center;
      background: rgba(17, 24, 39, 0.2);
    }}

    .expand-btn {{
      background: none;
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 6px 16px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }}

    .expand-btn:hover {{
      color: var(--text-main);
      border-color: rgba(255, 255, 255, 0.2);
      background: rgba(255, 255, 255, 0.03);
    }}

    .chevron-icon {{
      width: 14px;
      height: 14px;
      transition: transform 0.2s ease;
    }}

    .chevron-icon.rotated {{
      transform: rotate(180deg);
    }}

    .empty-state {{
      grid-column: 1 / -1;
      text-align: center;
      padding: 32px;
      color: var(--text-muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <span class="live-indicator"></span>
      <h1>Market Wire Hub</h1>
    </div>
    <input type="text" id="liveSearch" class="search-bar" placeholder="Search across all sectors..." />
  </header>

  <main class="main-feed-container">
    {sections_html}
  </main>

  <script>
    function toggleSection(catKey) {{
      const grid = document.getElementById('grid-' + catKey);
      const btn = document.getElementById('btn-' + catKey);
      const extras = grid.querySelectorAll('.card-extra');
      const chevron = btn.querySelector('.chevron-icon');
      const label = btn.querySelector('span');

      const isHidden = extras[0] && extras[0].style.display === 'none';

      extras.forEach(card => {{
        card.style.display = isHidden ? 'flex' : 'none';
      }});

      if (isHidden) {{
        chevron.classList.add('rotated');
        label.textContent = 'Show Fewer';
      }} else {{
        chevron.classList.remove('rotated');
        label.textContent = 'Show ' + extras.length + ' More Headlines';
      }}
    }}

    // Real-time Search Handler
    const search = document.getElementById('liveSearch');
    const allCards = document.querySelectorAll('.news-card');

    search.addEventListener('input', () => {{
      const query = search.value.toLowerCase().trim();

      allCards.forEach(card => {{
        const text = card.innerText.toLowerCase();
        if (!query) {{
          // Revert to collapsed state if empty
          if (card.classList.contains('card-extra')) {{
            const sec = card.closest('.collapsible-section');
            const btn = sec.querySelector('.expand-btn');
            const isExpanded = btn && btn.querySelector('.chevron-icon').classList.contains('rotated');
            card.style.display = isExpanded ? 'flex' : 'none';
          }} else {{
            card.style.display = 'flex';
          }}
        }} else {{
          card.style.display = text.includes(query) ? 'flex' : 'none';
        }}
      }});
    }});
  </script>
</body>
</html>"""

  with open(filename, 'w', encoding='utf-8') as f:
    f.write(full_html)

  print(f'Generated Stacked Collapsible dashboard in {filename}!')


if __name__ == '__main__':
  FEEDS = [
      # =========================================================================
      # 1. ECONOMY & CENTRAL BANKS
      # =========================================================================
      (
          'Federal Reserve - Releases',
          'economy',
          'https://www.federalreserve.gov/feeds/press_all.xml',
      ),
      (
          'Federal Reserve - Policy',
          'economy',
          'https://www.federalreserve.gov/feeds/press_monetary.xml',
      ),
      (
          'FT Global Economy',
          'economy',
          'https://www.ft.com/global-economy?format=rss',
      ),
      ('FT World News', 'economy', 'https://www.ft.com/world?format=rss'),
      (
          'Investing.com Indicators',
          'economy',
          'https://www.investing.com/rss/news_14.rss',
      ),
      (
          'SEC Press Releases',
          'economy',
          'https://www.sec.gov/news/pressreleases.rss',
      ),
      # =========================================================================
      # 2. MARKETS & EQUITIES
      # =========================================================================
      ('Yahoo Finance', 'markets', 'https://finance.yahoo.com/news/rssindex'),
      (
          'Seeking Alpha Currents',
          'markets',
          'https://seekingalpha.com/market_currents.xml',
      ),
      (
          'MarketWatch Top Stories',
          'markets',
          'https://feeds.content.dowjones.io/public/rss/mw_topstories',
      ),
      (
          'MarketWatch Pulse',
          'markets',
          'https://feeds.content.dowjones.io/public/rss/mw_bulletins',
      ),
      (
          'Investing.com Stocks',
          'markets',
          'https://www.investing.com/rss/news_25.rss',
      ),
      ('Benzinga', 'markets', 'https://www.benzinga.com/feed'),
      ('TechCrunch', 'markets', 'https://techcrunch.com/feed/'),
      ('OilPrice.com', 'markets', 'https://oilprice.com/rss/main'),
      # =========================================================================
      # 3. PRECIOUS METALS & MINING
      # =========================================================================
      ('GoldSeek', 'metals', 'https://news.goldseek.com/newsRSS.xml'),
      ('Mining.com', 'metals', 'https://www.mining.com/feed/'),
      (
          'Investing.com Gold',
          'metals',
          'https://www.investing.com/rss/news_289.rss',
      ),
      ('MiningFeeds', 'metals', 'https://www.miningfeeds.com/feed/'),
      ('King World News', 'metals', 'https://kingworldnews.com/feed/'),
      # =========================================================================
      # 4. CRYPTO & DIGITAL ASSETS
      # =========================================================================
      (
          'CoinDesk',
          'crypto',
          'https://www.coindesk.com/arc/outboundfeeds/rss/',
      ),
      ('Cointelegraph', 'crypto', 'https://cointelegraph.com/rss'),
      ('Decrypt', 'crypto', 'https://decrypt.co/feed'),
      ('Bitcoin Magazine', 'crypto', 'https://bitcoinmagazine.com/.rss/full/'),
  ]

  collected = []
  print(f'Fetching {len(FEEDS)} feeds across all sectors in parallel...')

  with ThreadPoolExecutor(max_workers=15) as executor:
    results = list(executor.map(scrape_fresh_articles, FEEDS))
    for res in results:
      collected.extend(res)

  generate_html_report(collected, filename='index.html')