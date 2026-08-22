from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import trafilatura

# Session Configuration
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
})

MAX_AGE_HOURS = 72
CACHE_FILE = "feed_cache.json"
CACHE_TTL_SECONDS = 900  # 15 mins

PAYWALLED_DOMAINS = [
    "wsj.com", "bloomberg.com", "ft.com", "nytimes.com", "barrons.com",
    "theinformation.com", "seekingalpha.com", "reuters.com"
]


def clean_text(raw_html: str) -> str:
    if not raw_html:
        return ''
    clean = re.sub(r'<.*?>', '', raw_html)
    return html.unescape(clean).strip()


def sanitize_xml(xml_content: str) -> str:
    if not xml_content:
        return ''
    xml_content = re.sub(r'<\?xml[^>]*\?>', '', xml_content, count=1)
    cleaned = re.sub(r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', xml_content)
    cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', cleaned)
    return cleaned.strip()


def is_paywalled_link(url: str, title: str) -> bool:
    if not url:
        return False
    lower_url = url.lower()
    lower_title = title.lower()
    if any(d in lower_url for d in PAYWALLED_DOMAINS):
        return True
    if "[subscriber]" in lower_title or "[paywall]" in lower_title:
        return True
    return False


def parse_universal_date(date_str: str) -> datetime:
    if not date_str:
        return datetime.now(timezone.utc)
    cleaned = date_str.strip()
    try:
        dt = parsedate_to_datetime(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        iso_clean = cleaned.replace('Z', '+00:00')
        dt = datetime.fromisoformat(iso_clean)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def clean_headline_tokens(title: str) -> set:
    cleaned = re.sub(r"[^\w\s]", "", title.lower())
    stop_words = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "is", "after", "with", "vs", "says", "as", "how", "what", "why"}
    return {w for w in cleaned.split() if w not in stop_words and len(w) > 2}


def is_duplicate(new_tokens: set, seen_token_sets: list, threshold: float = 0.75) -> bool:
    if not new_tokens:
        return False
    for existing_tokens in seen_token_sets:
        intersection = len(new_tokens.intersection(existing_tokens))
        union = len(new_tokens.union(existing_tokens))
        if union > 0 and (intersection / union) >= threshold:
            return True
    return False


def load_cache():
    default_cache = {"timestamp": 0, "articles": []}
    if not os.path.exists(CACHE_FILE):
        return default_cache, False
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                now = time.time()
                ts = data.get("timestamp", 0)
                is_valid = (now - ts) < CACHE_TTL_SECONDS
                return data, is_valid
    except Exception as e:
        print(f"Warning: Could not read cache ({e}). Resetting.")
    return default_cache, False


def save_cache(cache_data: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save cache: {e}")


def extract_full_body(article_url: str, fallback_desc: str) -> str:
    """Extracts body text cleanly with a strict 3-second timeout."""
    if not article_url:
        return f"<p>{fallback_desc}</p>" if fallback_desc else "<p>No content available.</p>"
    
    try:
        resp = SESSION.get(article_url, timeout=3)
        if resp.status_code == 200:
            extracted = trafilatura.extract(
                resp.text,
                include_comments=False,
                include_tables=False,
                no_fallback=False
            )
            if extracted and len(extracted.strip()) > 100:
                paragraphs = [p.strip() for p in extracted.split('\n') if p.strip()]
                return "".join(f"<p style='margin-bottom: 1.25em;'>{p}</p>" for p in paragraphs)
    except Exception:
        pass
    
    fallback_clean = fallback_desc.strip() if fallback_desc else "Summary not provided by publisher."
    return f"<p style='margin-bottom: 1.25em;'>{fallback_clean}</p>"


def scrape_fresh_articles(feed_tuple: tuple[str, str, str], max_age_hours=MAX_AGE_HOURS) -> list[dict]:
    source_name, category, feed_url = feed_tuple
    articles = []
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    try:
        resp = SESSION.get(feed_url, timeout=8)
        if resp.status_code != 200:
            print(f"⚠️ [HTTP {resp.status_code}] Skipping {source_name}")
            return []

        clean_xml_text = sanitize_xml(resp.text)
        
        try:
            root = ET.fromstring(clean_xml_text)
            is_bs4 = False
        except ET.ParseError:
            soup = BeautifulSoup(resp.content, "xml")
            is_bs4 = True

        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'dc': 'http://purl.org/dc/elements/1.1/',
            'content': 'http://purl.org/rss/1.0/modules/content/'
        }

        if not is_bs4:
            channel = root.find('channel')
            if channel is not None:
                for item in channel.findall('item'):
                    title = clean_text(item.findtext('title', default='No Title'))
                    link = item.findtext('link', default='').strip()
                    if not link or is_paywalled_link(link, title):
                        continue

                    raw_date = item.findtext('pubDate', default='') or item.findtext('dc:date', default='', namespaces=ns)
                    pub_dt = parse_universal_date(raw_date)
                    if pub_dt < cutoff_time:
                        continue

                    desc = clean_text(item.findtext('description', default=''))
                    articles.append({
                        'source': source_name,
                        'category': category.lower().strip(),
                        'title': title,
                        'link': link,
                        'iso_date': pub_dt.isoformat(),
                        'date_str': pub_dt.strftime('%b %d • %I:%M %p UTC'),
                        'desc': desc,
                    })

            elif 'feed' in root.tag.lower():
                atom_ns = {'atom': root.tag.split('}')[0].strip('{')}
                for entry in root.findall('atom:entry', atom_ns):
                    title = clean_text(entry.findtext('atom:title', default='No Title', namespaces=atom_ns))
                    link_elem = entry.find('atom:link', atom_ns)
                    link = link_elem.attrib.get('href', '').strip() if link_elem is not None else ''
                    if not link or is_paywalled_link(link, title):
                        continue

                    raw_date = entry.findtext('atom:published', default=entry.findtext('atom:updated', default='', namespaces=atom_ns), namespaces=atom_ns).strip()
                    pub_dt = parse_universal_date(raw_date)
                    if pub_dt < cutoff_time:
                        continue

                    desc = clean_text(entry.findtext('atom:summary', default=entry.findtext('atom:content', default='', namespaces=atom_ns), namespaces=atom_ns))
                    articles.append({
                        'source': source_name,
                        'category': category.lower().strip(),
                        'title': title,
                        'link': link,
                        'iso_date': pub_dt.isoformat(),
                        'date_str': pub_dt.strftime('%b %d • %I:%M %p UTC'),
                        'desc': desc,
                    })
        else:
            for item in soup.find_all(['item', 'entry']):
                title_tag = item.find(['title'])
                title = clean_text(title_tag.text) if title_tag else 'No Title'
                
                link_tag = item.find(['link'])
                link = ''
                if link_tag:
                    link = link_tag.get('href') or link_tag.text or ''
                link = link.strip()
                
                if not link or is_paywalled_link(link, title):
                    continue
                    
                date_tag = item.find(['pubDate', 'published', 'updated', 'dc:date'])
                raw_date = date_tag.text.strip() if date_tag else ''
                pub_dt = parse_universal_date(raw_date)
                
                if pub_dt < cutoff_time:
                    continue
                    
                desc_tag = item.find(['description', 'summary', 'content'])
                desc = clean_text(desc_tag.text) if desc_tag else ''
                
                articles.append({
                    'source': source_name,
                    'category': category.lower().strip(),
                    'title': title,
                    'link': link,
                    'iso_date': pub_dt.isoformat(),
                    'date_str': pub_dt.strftime('%b %d • %I:%M %p UTC'),
                    'desc': desc,
                })

    except Exception as e:
        print(f"⚠️ [Error] Skipping {source_name}: {e}")

    return articles


def deduplicate_articles(articles: list[dict]) -> list[dict]:
    """Deduplicates headlines within their respective category buckets."""
    deduped = []
    seen_urls = set()
    category_tokens = {"economy": [], "markets": [], "metals": [], "crypto": []}

    for art in articles:
        link = art.get('link', '')
        url_hash = hashlib.md5(link.encode('utf-8')).hexdigest() if link else None
        if url_hash and url_hash in seen_urls:
            continue

        cat = art.get('category', 'markets')
        tokens = clean_headline_tokens(art.get('title', ''))
        seen_token_sets = category_tokens.get(cat, [])

        if is_duplicate(tokens, seen_token_sets, threshold=0.75):
            continue

        if url_hash:
            seen_urls.add(url_hash)
        if cat in category_tokens:
            category_tokens[cat].append(tokens)

        deduped.append(art)

    return deduped


def enrich_article_with_full_text(art: dict) -> dict:
    art['full_text'] = extract_full_body(art['link'], art.get('desc', ''))
    return art


def generate_html_report(all_articles: list[dict], filename='index.html'):
    for a in all_articles:
        try:
            a['date_obj'] = datetime.fromisoformat(a['iso_date'])
        except Exception:
            a['date_obj'] = datetime.min.replace(tzinfo=timezone.utc)

    all_articles.sort(key=lambda x: x['date_obj'], reverse=True)

    json_payload = []
    for idx, a in enumerate(all_articles):
        json_payload.append({
            'id': idx,
            'source': a['source'],
            'category': a['category'],
            'title': a['title'],
            'link': a['link'],
            'date_str': a['date_str'],
            'full_text': a.get('full_text', a.get('desc', '')),
        })

    articles_json_str = json.dumps(json_payload)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Market News | Live Terminal & Reader</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-body: #07090e;
      --bg-surface: #0e131f;
      --bg-surface-hover: #151c2e;
      --bg-active: #1a233a;
      --border-color: #1e293b;
      --text-main: #f8fafc;
      --text-muted: #8492a6;
      --accent-economy: #10b981;
      --accent-markets: #38bdf8;
      --accent-crypto: #a855f7;
      --accent-metals: #eab308;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
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
      padding: 10px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-shrink: 0;
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}

    h1 {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.2px;
      text-transform: uppercase;
      font-family: 'JetBrains Mono', monospace;
    }}

    .live-indicator {{
      width: 7px;
      height: 7px;
      background: var(--accent-economy);
      border-radius: 50%;
      box-shadow: 0 0 8px var(--accent-economy);
    }}

    .search-bar {{
      background: var(--bg-body);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      width: 260px;
      outline: none;
      transition: border-color 0.2s;
    }}

    .search-bar:focus {{
      border-color: var(--accent-markets);
    }}

    .workspace {{
      display: flex;
      flex: 1;
      height: calc(100vh - 49px);
      overflow: hidden;
    }}

    /* Master Stream: 38% width */
    .master-pane {{
      width: 38%;
      min-width: 360px;
      max-width: 520px;
      border-right: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      background: var(--bg-body);
      flex-shrink: 0;
    }}

    .filter-bar {{
      padding: 8px 14px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      gap: 6px;
      overflow-x: auto;
      background: rgba(14, 19, 31, 0.7);
    }}

    .tab-pill {{
      background: none;
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 600;
      font-family: 'JetBrains Mono', monospace;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s ease;
    }}

    .tab-pill:hover {{
      color: var(--text-main);
      border-color: rgba(255, 255, 255, 0.25);
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
      margin-bottom: 6px;
    }}

    .row-tags {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .badge {{
      font-size: 8.5px;
      font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
      padding: 2px 5px;
      border-radius: 3px;
      letter-spacing: 0.4px;
    }}

    .badge-economy {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-economy); }}
    .badge-markets {{ background: rgba(56, 189, 248, 0.15); color: var(--accent-markets); }}
    .badge-crypto  {{ background: rgba(168, 85, 247, 0.15); color: var(--accent-crypto); }}
    .badge-metals  {{ background: rgba(234, 179, 8, 0.15); color: var(--accent-metals); }}

    .source-label {{
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 500;
    }}

    .time-label {{
      font-size: 10px;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
    }}

    .row-title {{
      font-size: 13px;
      font-weight: 600;
      line-height: 1.4;
      color: var(--text-main);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    /* Detail Pane: Expansive Reader */
    .detail-pane {{
      flex: 1;
      padding: 40px 60px;
      overflow-y: auto;
      background: var(--bg-surface);
      display: flex;
      flex-direction: column;
    }}

    .reader-container {{
      max-width: 780px;
      width: 100%;
      margin: 0 auto;
    }}

    .detail-meta {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
    }}

    .detail-title {{
      font-size: 28px;
      font-weight: 700;
      line-height: 1.3;
      margin-bottom: 24px;
      color: #ffffff;
      letter-spacing: -0.4px;
    }}

    .detail-body {{
      font-family: 'Newsreader', Georgia, serif;
      font-size: 18px;
      line-height: 1.8;
      color: #d1d5db;
      margin-bottom: 40px;
      white-space: pre-line;
    }}

    .action-bar {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding-top: 24px;
      border-top: 1px solid var(--border-color);
      margin-bottom: 60px;
    }}

    .primary-btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--accent-markets);
      color: #040914;
      font-weight: 600;
      font-size: 12px;
      font-family: 'JetBrains Mono', monospace;
      padding: 9px 16px;
      border-radius: 6px;
      text-decoration: none;
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

    @media (max-width: 860px) {{
      .workspace {{ flex-direction: column; }}
      .master-pane {{ width: 100%; max-width: none; height: 45%; }}
      .detail-pane {{ height: 55%; padding: 24px 20px; }}
      .detail-title {{ font-size: 20px; }}
      .detail-body {{ font-size: 16px; }}
    }}
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <span class="live-indicator"></span>
      <h1>Market News Wire</h1>
    </div>
    <input type="text" id="searchInput" class="search-bar" placeholder="Filter stories (e.g. Fed, Gold, BTC)..." />
  </header>

  <div class="workspace">
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

    <div class="detail-pane" id="detailPane">
      <div class="empty-state">Select a story from the wire to open full reader mode.</div>
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
        const artCat = (a.category || '').toLowerCase().trim();
        const filterCat = (currentFilter || '').toLowerCase().trim();
        const matchesCategory = filterCat === 'all' || artCat === filterCat;
        const matchesSearch = currentSearch === '' || 
          a.title.toLowerCase().includes(currentSearch) || 
          a.source.toLowerCase().includes(currentSearch);
        return matchesCategory && matchesSearch;
      }});

      if (filtered.length === 0) {{
        headlineStream.innerHTML = '<div class="empty-state" style="padding: 30px;">No matching articles in this category.</div>';
        detailPane.innerHTML = '<div class="empty-state">No matching story found.</div>';
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

      renderStream();

      detailPane.innerHTML = `
        <div class="reader-container">
          <div class="detail-meta">
            <span class="badge badge-${{article.category}}">${{article.category.toUpperCase()}}</span>
            <span class="source-label" style="font-size: 12px; font-weight: 600;">${{article.source}}</span>
            <span class="time-label">• ${{article.date_str}}</span>
          </div>
          <h2 class="detail-title">${{article.title}}</h2>
          <div class="detail-body">${{article.full_text}}</div>
          <div class="action-bar">
            <a href="${{article.link}}" target="_blank" rel="noopener noreferrer" class="primary-btn">
              Open Original Source ↗
            </a>
          </div>
        </div>
      `;
      detailPane.scrollTop = 0;
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

    renderStream();
    if (articles.length > 0) {{
      selectArticle(articles[0].id);
    }}
  </script>
</body>
</html>"""

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(full_html)

    print(f'Generated Full-Text Reader in {filename}!')


if __name__ == '__main__':
    FEEDS = [
        # 1. ECONOMY
        ('Federal Reserve - Releases', 'economy', 'https://www.federalreserve.gov/feeds/press_all.xml'),
        ('Federal Reserve - Policy', 'economy', 'https://www.federalreserve.gov/feeds/press_monetary.xml'),
        ('SEC Press Releases', 'economy', 'https://www.sec.gov/news/pressreleases.rss'),
        ('Bureau of Labor Statistics', 'economy', 'https://www.bls.gov/feed/bls_latest.rss'),
        ('Investing.com Indicators', 'economy', 'https://www.investing.com/rss/news_14.rss'),

        # 2. MARKETS
        ('Yahoo Finance', 'markets', 'https://finance.yahoo.com/news/rssindex'),
        ('Benzinga', 'markets', 'https://www.benzinga.com/feed'),
        ('TechCrunch', 'markets', 'https://techcrunch.com/feed/'),
        ('Investing.com Stocks', 'markets', 'https://www.investing.com/rss/news_25.rss'),

        # 3. METALS & COMMODITIES
        ('Mining.com', 'metals', 'https://www.mining.com/feed/'),
        ('OilPrice Commodities', 'metals', 'https://oilprice.com/rss/main'),
        ('Investing.com Commodities', 'metals', 'https://www.investing.com/rss/news_11.rss'),
        ('Investing.com Gold', 'metals', 'https://www.investing.com/rss/news_289.rss'),
        ('GoldSeek', 'metals', 'https://news.goldseek.com/newsRSS.xml'),
        ('SilverSeek', 'metals', 'https://silverseek.com/rss.xml'),

        # 4. CRYPTO
        ('Cointelegraph', 'crypto', 'https://cointelegraph.com/rss'),
        ('Bitcoin Magazine', 'crypto', 'https://bitcoinmagazine.com/.rss/full/'),
        ('CryptoSlate', 'crypto', 'https://cryptoslate.com/feed/'),
        ('CoinJournal', 'crypto', 'https://coinjournal.net/news/feed/'),
    ]

    cache_data, is_cache_valid = load_cache()

    if is_cache_valid and cache_data.get("articles"):
        print(f"Using cached reader data ({len(cache_data['articles'])} articles).")
        final_articles = cache_data["articles"]
    else:
        collected = []
        print(f'Fetching {len(FEEDS)} RSS feeds in parallel...')
        with ThreadPoolExecutor(max_workers=15) as executor:
            results = list(executor.map(scrape_fresh_articles, FEEDS))
            for res in results:
                collected.extend(res)

        print(f"Total raw items scraped: {len(collected)}")
        deduped = deduplicate_articles(collected)

        categories = {'economy': [], 'markets': [], 'metals': [], 'crypto': []}
        for art in deduped:
            cat = art.get('category', 'markets').lower().strip()
            if cat in categories:
                categories[cat].append(art)

        print("\n--- Scraped Article Counts per Sector ---")
        for cat, items in categories.items():
            print(f"  {cat.upper()}: {len(items)} available")
        print("-----------------------------------------\n")

        balanced_selection = []
        for cat, items in categories.items():
            balanced_selection.extend(items[:20])

        print(f"Extracting full reader text for {len(balanced_selection)} balanced articles...")
        with ThreadPoolExecutor(max_workers=12) as executor:
            enriched = list(executor.map(enrich_article_with_full_text, balanced_selection))

        final_articles = enriched
        save_cache({"timestamp": time.time(), "articles": final_articles})

    generate_html_report(final_articles, filename='index.html')