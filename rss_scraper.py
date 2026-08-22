from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import os
import re
import ssl
import urllib.request
import webbrowser
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
  """Removes raw HTML tags and decodes entities like &amp; or &#39;"""
  if not raw_html:
    return ''
  clean = re.sub(r'<.*?>', '', raw_html)
  return html.unescape(clean).strip()


def parse_universal_date(date_str: str) -> datetime | None:
  """Parses RFC-822, RFC-3339, and ISO-8601 dates into a UTC datetime object."""
  if not date_str:
    return None

  cleaned = date_str.strip()

  # 1. Standard RSS RFC-822
  try:
    dt = parsedate_to_datetime(cleaned)
    if dt.tzinfo is None:
      return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
  except Exception:
    pass

  # 2. ISO-8601 / RFC-3339
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
      xml_data = response.read()

    root = ET.fromstring(xml_data)

    # 1. RSS 2.0 (<channel><item>)
    channel = root.find("channel")
    if channel is not None:
      for item in channel.findall("item"):
        raw_date = item.findtext("pubDate", default="").strip()
        pub_dt = parse_universal_date(raw_date)

        if pub_dt and pub_dt < cutoff_time:
          continue

        articles.append({
            "source": source_name,
            "category": category,
            "title": clean_text(item.findtext("title", default="No Title")),
            "link": item.findtext("link", default="").strip(),
            "date_obj": pub_dt or datetime.min.replace(tzinfo=timezone.utc),
            "date_str": pub_dt.strftime("%b %d • %I:%M %p UTC")
            if pub_dt
            else "Recent",
            "desc": clean_text(item.findtext("description", default=""))[:220]
            + "...",
        })

    # 2. Atom (<feed><entry>)
    elif "feed" in root.tag.lower():
      ns = {"atom": root.tag.split("}")[0].strip("{")}
      for entry in root.findall("atom:entry", ns):
        raw_date = entry.findtext(
            "atom:published",
            default=entry.findtext("atom:updated", default="", namespaces=ns),
            namespaces=ns,
        ).strip()
        pub_dt = parse_universal_date(raw_date)

        if pub_dt and pub_dt < cutoff_time:
          continue

        link_elem = entry.find("atom:link", ns)
        link = (
            link_elem.attrib.get("href", "").strip()
            if link_elem is not None
            else ""
        )

        articles.append({
            "source": source_name,
            "category": category,
            "title": clean_text(
                entry.findtext("atom:title", default="No Title", namespaces=ns)
            ),
            "link": link,
            "date_obj": pub_dt or datetime.min.replace(tzinfo=timezone.utc),
            "date_str": pub_dt.strftime("%b %d • %I:%M %p UTC")
            if pub_dt
            else "Recent",
            "desc": clean_text(
                entry.findtext(
                    "atom:summary",
                    default=entry.findtext(
                        "atom:content", default="", namespaces=ns
                    ),
                    namespaces=ns,
                )
            )[:220]
            + "...",
        })

  except Exception as e:
    print(f"Error fetching {source_name}: {e}")

  return articles


def generate_html_report(all_articles: list[dict], filename="index.html"):
  # Sort all articles newest to oldest
  all_articles.sort(key=lambda x: x["date_obj"], reverse=True)

  # Partition articles by section
  sections_data = {
      "economy": {
          "title": "🏛️ Economy & Macro",
          "items": [],
          "badge_class": "badge-economy",
      },
      "markets": {
          "title": "📈 Markets & Equities",
          "items": [],
          "badge_class": "badge-markets",
      },
      "crypto": {
          "title": "⚡ Crypto & Digital Assets",
          "items": [],
          "badge_class": "badge-crypto",
      },
  }

  for a in all_articles:
    cat = a.get("category", "markets")
    if cat in sections_data:
      sections_data[cat]["items"].append(a)

  # Render section HTML
  sections_html = ""
  for key, sec in sections_data.items():
    cards_markup = ""
    for a in sec["items"]:
      cards_markup += f"""
      <article class="news-card">
        <div class="card-meta">
          <span class="badge {sec['badge_class']}">{a['source']}</span>
          <time class="card-time">{a['date_str']}</time>
        </div>
        <h3 class="card-title">
          <a href="{a['link']}" target="_blank" rel="noopener noreferrer">{a['title']}</a>
        </h3>
        <p class="card-desc">{a['desc']}</p>
      </article>
      """
    if not sec["items"]:
      cards_markup = (
          '<p class="empty-state">No articles in the past 24 hours.</p>'
      )

    sections_html += f"""
    <section class="feed-column" id="{key}">
      <div class="section-header">
        <h2>{sec['title']}</h2>
        <span class="section-count">{len(sec['items'])} items</span>
      </div>
      <div class="card-stream">
        {cards_markup}
      </div>
    </section>
    """

  full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Market Terminal Feed</title>
  <style>
    :root {{
      --bg-body: #090d16;
      --bg-surface: #111827;
      --bg-surface-hover: #1b2436;
      --border-color: #1f293d;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent-economy: #34d399;
      --accent-markets: #38bdf8;
      --accent-crypto: #fbbf24;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg-body);
      color: var(--text-main);
      padding: 24px;
    }}

    header {{
      max-width: 1500px;
      margin: 0 auto 24px auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border-color);
    }}

    h1 {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.5px;
      display: flex;
      align-items: center;
      gap: 10px;
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

    /* 3-Column Layout */
    .dashboard-grid {{
      max-width: 1500px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
      gap: 24px;
      align-items: start;
    }}

    .feed-column {{
      background: rgba(17, 24, 39, 0.4);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      overflow: hidden;
    }}

    .section-header {{
      background: var(--bg-surface);
      padding: 14px 18px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 10;
    }}

    .section-header h2 {{
      font-size: 15px;
      font-weight: 600;
    }}

    .section-count {{
      font-size: 11px;
      color: var(--text-muted);
      background: rgba(255, 255, 255, 0.05);
      padding: 2px 8px;
      border-radius: 12px;
    }}

    .card-stream {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding: 14px;
    }}

    .news-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 14px 16px;
      transition: transform 0.15s ease, background 0.15s ease;
    }}

    .news-card:hover {{
      background: var(--bg-surface-hover);
      transform: translateY(-1px);
    }}

    .card-meta {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}

    .badge {{
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      padding: 2px 6px;
      border-radius: 4px;
      letter-spacing: 0.5px;
    }}

    .badge-economy {{ background: rgba(52, 211, 153, 0.15); color: var(--accent-economy); }}
    .badge-markets {{ background: rgba(56, 189, 248, 0.15); color: var(--accent-markets); }}
    .badge-crypto  {{ background: rgba(251, 191, 36, 0.15); color: var(--accent-crypto); }}

    .card-time {{
      font-size: 11px;
      color: var(--text-muted);
    }}

    .card-title {{
      font-size: 14px;
      line-height: 1.4;
      margin-bottom: 6px;
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

    .empty-state {{
      text-align: center;
      padding: 30px;
      color: var(--text-muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>

  <header>
    <h1><span class="live-indicator"></span> Financial News Hub</h1>
    <input type="text" id="liveSearch" class="search-bar" placeholder="Filter all sections (e.g. CPI, BTC, AAPL)..." />
  </header>

  <main class="dashboard-grid">
    {sections_html}
  </main>

  <script>
    const search = document.getElementById('liveSearch');
    const cards = document.querySelectorAll('.news-card');

    search.addEventListener('input', () => {{
      const query = search.value.toLowerCase();
      cards.forEach(card => {{
        const text = card.innerText.toLowerCase();
        card.style.display = text.includes(query) ? 'block' : 'none';
      }});
    }});
  </script>
</body>
</html>"""

  with open(filename, "w", encoding="utf-8") as f:
    f.write(full_html)

  # webbrowser.open("file://" + os.path.realpath(filename))
  print(f"Generated 3-section dashboard in {filename}!")


if __name__ == '__main__':
  FEEDS = [
    # =========================================================================
    # 1. ECONOMY & CENTRAL BANKS
    # =========================================================================
    (
        "Federal Reserve - All",
        "economy",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ),
    (
        "Federal Reserve - Policy",
        "economy",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
    ),
    ("FT Global Economy", "economy", "https://www.ft.com/global-economy?format=rss"),
    ("FT World News", "economy", "https://www.ft.com/world?format=rss"),
    (
        "Investing.com - Eco Indicators",
        "economy",
        "https://www.investing.com/rss/news_14.rss",
    ),
    ("SEC Press Releases", "economy", "https://www.sec.gov/news/pressreleases.rss"),
    # =========================================================================
    # 2. MARKETS & EQUITIES
    # =========================================================================
    ("Yahoo Finance", "markets", "https://finance.yahoo.com/news/rssindex"),
    (
        "Seeking Alpha Currents",
        "markets",
        "https://seekingalpha.com/market_currents.xml",
    ),
    (
        "MarketWatch Top Stories",
        "markets",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    ),
    (
        "MarketWatch Pulse",
        "markets",
        "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
    ),
    ("Investing.com Stocks", "markets", "https://www.investing.com/rss/news_25.rss"),
    ("Benzinga", "markets", "https://www.benzinga.com/feed"),
    ("TechCrunch", "markets", "https://techcrunch.com/feed/"),
    ("OilPrice.com", "markets", "https://oilprice.com/rss/main"),
    # =========================================================================
    # 3. CRYPTO & DIGITAL ASSETS
    # =========================================================================
    (
        "CoinDesk",
        "crypto",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ),
    ("Cointelegraph", "crypto", "https://cointelegraph.com/rss"),
    ("Decrypt", "crypto", "https://decrypt.co/feed"),
    ("Bitcoin Magazine", "crypto", "https://bitcoinmagazine.com/.rss/full/"),
]

  collected = []
  print(f'Fetching {len(FEEDS)} feeds in parallel...')

  with ThreadPoolExecutor(max_workers=15) as executor:
    results = list(executor.map(scrape_fresh_articles, FEEDS))
    for res in results:
      collected.extend(res)

  generate_html_report(collected)