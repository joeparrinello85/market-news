from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
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


def clean_text(raw_html: str) -> str:
  if not raw_html:
    return ''
  clean = re.sub(r'<.*?>', '', raw_html)
  return html.unescape(clean).strip()


def sanitize_xml(xml_content: str) -> str:
  """Sanitizes malformed XML entities, bad encodings, and control characters."""
  if not xml_content:
    return ''
  # Strip XML declaration line to avoid encoding mismatch issues during string parsing
  xml_content = re.sub(r'<\?xml[^>]*\?>', '', xml_content, count=1)
  # Fix bare unescaped ampersands
  cleaned = re.sub(
      r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', xml_content
  )
  # Remove ASCII control characters (keeps tab, newline, cr)
  cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', cleaned)
  return cleaned.strip()


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
                clean_text(item.findtext('description', default=''))[:400]
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
                )[:400]
                + '...'
            ),
        })

  except Exception as e:
    print(f'Error fetching {source_name}: {e}')

  return articles


def generate_html_report(all_articles: list[dict], filename='index.html'):
  all_articles.sort(key=lambda x: x['date_obj'], reverse=True)

  # Assign clean indices for JS selection
  json_payload = []
  for idx, a in enumerate(all_articles):
    json_payload.append({
        'id': idx,
        'source': a['source'],
        'category': a['category'],
        'title': a['title'],
        'link': a['link'],
        'date_str': a['date_str'],
        'desc': a['desc'],
    })

  articles_json_str = json.dumps(json_payload)

  full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Market-News</title>
  <style>
    :root {{
      --bg-body: #090d16;
      --bg-surface: #111827;
      --bg-surface-hover: #172136;
      --bg-active: #1e2a44;
      --border-color: #1e293b;
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
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    header {{
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border-color);
      padding: 12px 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-shrink: 0;
      gap: 16px;
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    h1 {{
      font-size: 17px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }}

    .live-indicator {{
      width: 8px;
      height: 8px;
      background: var(--accent-economy);
      border-radius: 50%;
      box-shadow: 0 0 8px var(--accent-economy);
    }}

    .search-bar {{
      background: var(--bg-body);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 13px;
      width: 280px;
      outline: none;
      transition: border-color 0.2s;
    }}

    .search-bar:focus {{
      border-color: var(--accent-markets);
    }}

    /* Main Workspace Layout */
    .workspace {{
      display: flex;
      flex: 1;
      height: calc(100vh - 57px);
      overflow: hidden;
    }}

    /* Left Master Pane: List */
    .master-pane {{
      width: 440px;
      border-right: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      background: var(--bg-body);
      flex-shrink: 0;
    }}

    .filter-bar {{
      padding: 10px 14px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      gap: 6px;
      overflow-x: auto;
      background: rgba(17, 24, 39, 0.6);
    }}

    .tab-pill {{
      background: none;
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 4px 10px;
      border-radius: 14px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s ease;
    }}

    .tab-pill:hover {{
      color: var(--text-main);
      border-color: rgba(255, 255, 255, 0.2);
    }}

    .tab-pill.active {{
      background: var(--text-main);
      color: var(--bg-body);
      border-color: var(--text-main);
    }}

    .headline-stream {{
      flex: 1;
      overflow-y: auto;
    }}

    .headline-row {{
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-color);
      cursor: pointer;
      transition: background 0.1s ease;
    }}

    .headline-row:hover {{
      background: var(--bg-surface-hover);
    }}

    .headline-row.active {{
      background: var(--bg-active);
      border-left: 3px solid var(--accent-markets);
    }}

    .row-meta {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 5px;
    }}

    .row-tags {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .badge {{
      font-size: 8px;
      font-weight: 700;
      padding: 2px 5px;
      border-radius: 3px;
      letter-spacing: 0.5px;
    }}

    .badge-economy {{ background: rgba(52, 211, 153, 0.15); color: var(--accent-economy); }}
    .badge-markets {{ background: rgba(56, 189, 248, 0.15); color: var(--accent-markets); }}
    .badge-crypto  {{ background: rgba(192, 132, 252, 0.15); color: var(--accent-crypto); }}
    .badge-metals  {{ background: rgba(234, 179, 8, 0.15); color: var(--accent-metals); }}

    .source-label {{
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 500;
    }}

    .time-label {{
      font-size: 10px;
      color: var(--text-muted);
    }}

    .row-title {{
      font-size: 13px;
      font-weight: 600;
      line-height: 1.35;
      color: var(--text-main);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    /* Right Detail Pane: Reader */
    .detail-pane {{
      flex: 1;
      padding: 36px 44px;
      overflow-y: auto;
      background: var(--bg-surface);
      display: flex;
      flex-direction: column;
      max-width: 900px;
    }}

    .detail-meta {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }}

    .detail-title {{
      font-size: 22px;
      font-weight: 700;
      line-height: 1.35;
      margin-bottom: 20px;
      color: var(--text-main);
    }}

    .detail-body {{
      font-size: 15px;
      line-height: 1.7;
      color: #cbd5e1;
      margin-bottom: 30px;
    }}

    .primary-btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: var(--accent-markets);
      color: #000;
      font-weight: 600;
      font-size: 13px;
      padding: 10px 20px;
      border-radius: 6px;
      text-decoration: none;
      width: fit-content;
      transition: opacity 0.15s ease;
    }}

    .primary-btn:hover {{
      opacity: 0.9;
    }}

    .empty-state {{
      margin: auto;
      text-align: center;
      color: var(--text-muted);
      font-size: 14px;
    }}

    @media (max-width: 800px) {{
      .workspace {{
        flex-direction: column;
      }}
      .master-pane {{
        width: 100%;
        height: 50%;
      }}
      .detail-pane {{
        height: 50%;
        padding: 20px;
      }}
    }}
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <span class="live-indicator"></span>
      <h1>Market News</h1>
    </div>
    <input type="text" id="searchInput" class="search-bar" placeholder="Filter stream..." />
  </header>

  <div class="workspace">
    <!-- Master Column -->
    <div class="master-pane">
      <div class="filter-bar">
        <button class="tab-pill active" onclick="filterCategory('all', this)">ALL</button>
        <button class="tab-pill" onclick="filterCategory('economy', this)">ECONOMY</button>
        <button class="tab-pill" onclick="filterCategory('markets', this)">MARKETS</button>
        <button class="tab-pill" onclick="filterCategory('metals', this)">METALS</button>
        <button class="tab-pill" onclick="filterCategory('crypto', this)">CRYPTO</button>
      </div>
      <div class="headline-stream" id="headlineStream"></div>
    </div>

    <!-- Detail Pane -->
    <div class="detail-pane" id="detailPane">
      <div class="empty-state">Select a headline from the stream to read details.</div>
    </div>
  </div>

  <script>
    const articles = {articles_json_str};
    let currentFilter = 'all';
    let currentSearch = '';
    let selectedId = articles.length > 0 ? articles[0].id : null;

    const headlineStream = document.getElementById('headlineStream');
    const detailPane = document.getElementById('detailPane');
    const searchInput = document.getElementById('searchInput');

    function renderStream() {{
      headlineStream.innerHTML = '';
      
      const filtered = articles.filter(a => {{
        const matchesCategory = currentFilter === 'all' || a.category === currentFilter;
        const matchesSearch = currentSearch === '' || 
          a.title.toLowerCase().includes(currentSearch) || 
          a.source.toLowerCase().includes(currentSearch);
        return matchesCategory && matchesSearch;
      }});

      if (filtered.length === 0) {{
        headlineStream.innerHTML = '<div class="empty-state" style="padding: 30px;">No matching articles.</div>';
        detailPane.innerHTML = '<div class="empty-state">No matching article selected.</div>';
        return;
      }}

      filtered.forEach(a => {{
        const row = document.createElement('div');
        row.className = `headline-row ${{a.id === selectedId ? 'active' : ''}}`;
        row.onclick = () => selectArticle(a.id);
        row.innerHTML = `
          <div class="row-meta">
            <div class="row-tags">
              <span class="badge badge-${{a.category}}">${{a.category.toUpperCase()}}</span>
              <span class="source-label">${{a.source}}</span>
            </div>
            <span class="time-label">${{a.date_str}}</span>
          </div>
          <div class="row-title">${{a.title}}</div>
        `;
        headlineStream.appendChild(row);
      }});

      if (!filtered.some(a => a.id === selectedId) && filtered.length > 0) {{
        selectArticle(filtered[0].id);
      }}
    }}

    function selectArticle(id) {{
      selectedId = id;
      const article = articles.find(a => a.id === id);
      if (!article) return;

      document.querySelectorAll('.headline-row').forEach(row => {{
        row.classList.remove('active');
      }});

      // Re-highlight active
      renderStream();

      detailPane.innerHTML = `
        <div class="detail-meta">
          <span class="badge badge-${{article.category}}">${{article.category.toUpperCase()}}</span>
          <span class="source-label" style="font-size: 13px; font-weight: 600;">${{article.source}}</span>
          <span class="time-label" style="font-size: 12px;">• ${{article.date_str}}</span>
        </div>
        <h2 class="detail-title">${{article.title}}</h2>
        <p class="detail-body">${{article.desc}}</p>
        <a href="${{article.link}}" target="_blank" rel="noopener noreferrer" class="primary-btn">
          Read Original Article ↗
        </a>
      `;
    }}

    function filterCategory(cat, btn) {{
      currentFilter = cat;
      document.querySelectorAll('.tab-pill').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      renderStream();
    }}

    searchInput.addEventListener('input', () => {{
      currentSearch = searchInput.value.toLowerCase().trim();
      renderStream();
    }});

    // Initialize
    renderStream();
    if (articles.length > 0) {{
      selectArticle(articles[0].id);
    }}
  </script>
</body>
</html>"""

  with open(filename, 'w', encoding='utf-8') as f:
    f.write(full_html)

  print(f'Generated Master-Detail reader in {filename}!')


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
    ("GoldSeek", "metals", "https://news.goldseek.com/newsRSS.xml"),
    ("SilverSeek", "metals", "https://silverseek.com/rss.xml"),
    ("Mining.com", "metals", "https://www.mining.com/feed/"),
    (
        "Investing.com Gold",
        "metals",
        "https://www.investing.com/rss/news_289.rss",
    ),
    (
        "INN Gold",
        "metals",
        "https://investingnews.com/category/daily/resource-investing/precious-metals-investing/gold-investing/feed/",
    ),
    (
        "INN Silver",
        "metals",
        "https://investingnews.com/category/daily/resource-investing/precious-metals-investing/silver-investing/feed/",
    ),
    ("The Northern Miner", "metals", "https://www.northernminer.com/feed/"),
    ("MiningFeeds", "metals", "https://www.miningfeeds.com/feed/"),
    ("BullionStar", "metals", "https://www.bullionstar.com/rss"),
    ("TF Metals Report", "metals", "https://www.tfmetalsreport.com/rss.xml"),
    ("King World News", "metals", "https://kingworldnews.com/feed/"),
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