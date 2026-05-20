import requests
import re
import time
import os
import warnings
import threading as _threading
from urllib.parse import urljoin, quote
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

HEADERS = {
    "User-Agent": "LogoScraper/1.0 (Visual Capitalist research; python-requests/2.31)",
    "Accept-Language": "en-US,en;q=0.9",
}

LOGOS_DIR = os.path.join(os.path.dirname(__file__), "logos")
os.makedirs(LOGOS_DIR, exist_ok=True)

SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update(HEADERS)
# Larger connection pool for concurrent workers
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

_RASTER_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_SVG_EXT = ".svg"


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _get(url, **kwargs):
    kwargs.setdefault("timeout", 8)
    for attempt in range(3):
        try:
            r = SESSION.get(url, **kwargs)
            if r.status_code == 429:
                time.sleep(2 ** attempt * 2)
                continue
            return r
        except Exception:
            return None
    return None


def _call_with_timeout(fn, company, kwargs, seconds=10):
    """Run a strategy function with a hard wall-clock timeout (Windows-safe).
    Returns None if the strategy takes longer than *seconds* or raises."""
    import threading
    result_box = [None]
    def _target():
        try:
            result_box[0] = fn(company, **kwargs)
        except Exception:
            pass
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(seconds)
    return result_box[0]


# ── Shared utilities ──────────────────────────────────────────────────────────

def safe_filename(name):
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


_NOISE = re.compile(
    r'\b(corporation|corp|inc|ltd|llc|co\.|company|group|holdings|'
    r'financial|enterprises?|markets|supply|services?|solutions?|'
    r'global|worldwide|international|national|american|usa|realty|'
    r'beverage|beverages?|foods?|products?|manufacturing|industries|'
    r'systems|technologies|partners|associates|resorts?|hotels?|'
    r'stores?|grocery|supermarkets?|tires?|automotive|bancshares|'
    r'bankshares|bancorp|insurance|energy|resources|healthcare|'
    r'communications|entertainment|the)\b',
    re.I,
)

_SKIP_FILES = re.compile(
    r'(commons[-_]logo|wikidata|wikiquote|wikinews|wikisource|wikivoyage|'
    r'wikiversity|wiktionary|mediawiki|wikipedia|oojs|ooui|'
    r'edit[-_]|icon[-_]|[-_]icon|arrow|button|symbol|placeholder|'
    r'checkmark|search|close|menu|chat|star|heart|share|bell|'
    r'stub|flag|coat[-_]of|location|increase|decrease|industry|'
    r'ambox|foodlogo|nuvola|blank|default|generic)',
    re.I,
)


def _sig_words(company):
    words = set(re.sub(r'[^a-z0-9]', ' ', company.lower()).split())
    words -= {'the', 'of', 'and', 'a', 'an', 'in', 'at', 'for',
              'inc', 'llc', 'ltd', 'co', 'usa', 'us'}
    # Preserve short uppercase abbreviations like "GE", "MGM", "3M"
    abbrevs = {w.lower() for w in company.split()
               if len(w) >= 2 and w.upper() == w and w.isalpha()}
    return {w for w in words if len(w) > 2} | (words & abbrevs)


def _disambig_ok(title, cwords):
    """
    Guard against short abbreviations matching wrong brands in filenames/URLs.
    If the company has BOTH short words (≤3 chars, e.g. "ge", "mgm") AND longer
    disambiguating words (e.g. "aerospace", "resorts"), require at least one
    long word to appear in the title.  Prevents "MGM_logo.svg" from matching
    "MGM Resorts International", or "GE_logo.svg" matching "GE Aerospace".
    """
    abbrev_words = {w for w in cwords if len(w) <= 3}
    long_words   = cwords - abbrev_words
    if not (abbrev_words and long_words):
        return True
    tl       = title.lower()
    tl_plain = re.sub(r'[^a-z0-9]', '', tl)   # e.g. "hyveelogosvg"
    return any(w in tl or w in tl_plain for w in long_words)


def _slug(text):
    """Lowercase hyphenated slug, noise stripped."""
    s = re.sub(r"'s?", '', text.lower())
    s = _NOISE.sub('', s).strip()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def _slug_plain(text):
    s = re.sub(r"'s?", '', text.lower())
    s = _NOISE.sub('', s).strip()
    return re.sub(r'[^a-z0-9]', '', s)


# ── Wikipedia / search helpers ───────────────────────────────────────────────

def _wiki_search(query, limit=3):
    r = _get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(limit), "format": "json",
    })
    if r and r.status_code == 200:
        return [h["title"] for h in r.json().get("query", {}).get("search", [])]
    return []


def _wiki_logo_files(article_title, company, extensions):
    """Return logo image files from a Wikipedia article, scored by relevance."""
    r = _get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "titles": article_title,
        "prop": "images", "imlimit": "50", "format": "json",
    })
    if not r or r.status_code != 200:
        return []
    pages = r.json().get("query", {}).get("pages", {})
    for page in pages.values():
        candidates = [
            img["title"] for img in page.get("images", [])
            if any(img["title"].lower().endswith(ext) for ext in extensions)
            and not _SKIP_FILES.search(img["title"])
            and "logo" in img["title"].lower()
        ]
        cwords = _sig_words(company)

        def _score(fname):
            fl = fname.lower()
            return sum(1 for w in cwords if w in fl)

        scored = sorted(candidates, key=_score, reverse=True)
        # Require ≥1 company sig_word AND pass abbreviation disambiguation.
        return [f for f in scored if _score(f) > 0 and _disambig_ok(f, cwords)]
    return []


def _wiki_file_url(file_title):
    r = _get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "titles": file_title,
        "prop": "imageinfo", "iiprop": "url", "format": "json",
    })
    if not r or r.status_code != 200:
        return None
    pages = r.json().get("query", {}).get("pages", {})
    for page in pages.values():
        url = page.get("imageinfo", [{}])[0].get("url", "")
        if url:
            return url
    return None


def _wiki_queries(company, category, state):
    """Ordered list of Wikipedia search queries to try (most specific first)."""
    queries = [company]
    if category:
        queries.append(f"{company} {category}")
    if state:
        queries.append(f"{company} {state}")
    return queries


def _wiki_article_data(article, cwords):
    """
    Single API call: fetch images list + pageimage for one article.
    Returns (images_list, pageimage_url) or ([], None).
    Skips articles whose title doesn't contain any company sig_word.
    """
    if cwords and not any(w in article.lower() for w in cwords):
        return [], None
    r = _get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "titles": article,
        "prop": "images|pageimages",
        "imlimit": "50", "piprop": "original",
        "format": "json",
    }, timeout=10)
    if not r or r.status_code != 200:
        return [], None
    pages = r.json().get("query", {}).get("pages", {})
    for page in pages.values():
        imgs  = [i["title"] for i in page.get("images", [])]
        pi    = page.get("original", {}).get("source", "")
        return imgs, pi
    return [], None


# ── SVG Strategy 1: VectorLogo.zone horizontal wordmark (ar21) ───────────────
# The -ar21.svg format is the horizontal brand lockup (symbol + wordmark text).
# Even when text is path-converted, this IS the wordmark so we mark it verified.

def _vlz_slugs(company):
    """All slug variants to try on VectorLogo.zone, most specific first."""
    variants = []
    for s in [_slug(company), _slug_plain(company)]:
        if s and s not in variants:
            variants.append(s)
    # Maximal-stripped slug (e.g. "publix" from "Publix Super Markets")
    domains = _company_domains(company)
    if domains:
        core = domains[-1]          # maximal-stripped is last in the list
        for v in [core, re.sub(r'[^a-z0-9]+', '-', core).strip('-')]:
            if v and len(v) > 2 and v not in variants:
                variants.append(v)
    # Also try first-word-only
    first = re.sub(r'[^a-z0-9]', '', company.lower().split()[0])
    if first and len(first) > 2 and first not in variants:
        variants.append(first)
    return variants

def try_vectorlogo_wordmark(company, **_):
    for slug in _vlz_slugs(company):
        url = f"https://www.vectorlogo.zone/logos/{slug}/{slug}-ar21.svg"
        r = _get(url, timeout=4)
        if r and r.status_code == 200 and "svg" in r.headers.get("Content-Type", ""):
            return {"url": url, "source": "VectorLogo.zone", "_wordmark": True}
    return None


# ── SVG Strategy 2: Simple Icons ─────────────────────────────────────────────

def try_simple_icons(company, **_):
    seen = set()
    variants = [
        _slug_plain(company),
        _slug(company),
        re.sub(r'[^a-z0-9]', '', company.lower()),
        # first significant word only (e.g. "mgm" from "MGM Resorts International")
        re.sub(r'[^a-z0-9]', '', company.lower().split()[0]),
        # without trailing noise words
        _slug_plain(re.sub(r'\s+(inc|llc|corp|ltd|co|the|group|holdings|'
                           r'international|national|american|enterprises|'
                           r'resorts|properties|financial|services)\s*$',
                           '', company, flags=re.I).strip()),
    ]
    for variant in variants:
        if not variant or variant in seen or len(variant) < 2:
            continue
        seen.add(variant)
        r = _get(f"https://cdn.simpleicons.org/{variant}", timeout=4)
        if r and r.status_code == 200 and "svg" in r.headers.get("Content-Type", ""):
            return {"url": f"https://cdn.simpleicons.org/{variant}",
                    "source": "Simple Icons", "_content": r.text}
    return None


# ── SVG Strategy 3a: VectorLogo.zone icon variant (fallback) ─────────────────

def try_vectorlogo_icon(company, **_):
    for slug in _vlz_slugs(company):
        url = f"https://www.vectorlogo.zone/logos/{slug}/{slug}-icon.svg"
        r = _get(url, timeout=4)
        if r and r.status_code == 200 and "svg" in r.headers.get("Content-Type", ""):
            return {"url": url, "source": "VectorLogo.zone"}
    return None


# ── SVG Strategy 3: Wikipedia (API-only — images list + pageimage) ───────────
# One combined API call per article; no HTML fetching.
# Handles both logo-named files (high confidence) and pageimage SVG (fallback).

def try_wikipedia_svg(company, category="", state=""):
    cwords = _sig_words(company)
    for query in _wiki_queries(company, category, state)[:1]:
        for article in _wiki_search(query, limit=1):
            time.sleep(0.15)
            imgs, pi = _wiki_article_data(article, cwords)

            # 1. Logo-named SVG files with a company sig_word in the filename
            for title in imgs:
                tl = title.lower()
                if not tl.endswith(".svg") or _SKIP_FILES.search(title):
                    continue
                if "logo" not in tl:
                    continue
                if cwords and not any(w in tl for w in cwords):
                    continue
                if not _disambig_ok(title, cwords):
                    continue
                if _title_extra_words(title, cwords) > 1:
                    continue
                url = _wiki_file_url(title)
                if url:
                    # Filename already confirmed company identity via sig_word filter —
                    # mark as reliable so "unverified" SVGs get kept as "meta_only".
                    return {"url": url, "source": "Wikipedia", "_reliable_source": True}

            # 2. Pageimage fallback — if it's an SVG
            # Wikipedia chose this as the article's representative image, so
            # treat it as a reliable source even if the URL check doesn't fire.
            if pi and pi.lower().split("?")[0].endswith(".svg"):
                if not _SKIP_FILES.search(pi.lower()):
                    return {"url": pi, "source": "Wikipedia",
                            "_reliable_source": True}
    return None


# ── SVG Strategy 6: Wikimedia Commons (SVG logos only) ───────────────────────

def _commons_file_url(title):
    r = _get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "titles": title,
        "prop": "imageinfo", "iiprop": "url", "format": "json",
    })
    if not r or r.status_code != 200:
        return None
    pages = r.json().get("query", {}).get("pages", {})
    for page in pages.values():
        url = page.get("imageinfo", [{}])[0].get("url", "")
        if url:
            return url
    return None


def try_wikimedia_commons_svg(company, category="", state=""):
    cwords = _sig_words(company)
    for query in [f"{company} logo"]:   # one query only — avoids rate-limit stacking
        if not query:
            continue
        r = _get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "list": "search",
            "srsearch": query, "srnamespace": "6",
            "srlimit": "10", "format": "json",
        })
        if not r or r.status_code != 200:
            continue
        for item in r.json().get("query", {}).get("search", []):
            title = item["title"]
            if not title.lower().endswith(_SVG_EXT):
                continue
            if _SKIP_FILES.search(title) or "logo" not in title.lower():
                continue
            if not _disambig_ok(title, cwords):
                continue
            # Skip sub-brand/product logos (e.g. "Michelin PAX System logo") that
            # have extra significant words beyond the company name.
            if _title_extra_words(title, cwords) > 1:
                continue
            time.sleep(0.2)
            url = _commons_file_url(title)
            if url:
                return {"url": url, "source": "Wikimedia Commons", "_reliable_source": True}
    return None


# ── SVG Strategy 7: WorldVectorLogo ──────────────────────────────────────────

def try_worldvectorlogo(company, **_):
    r = _get(f"https://worldvectorlogo.com/search?q={quote(company)}")
    if not r or r.status_code != 200:
        return None
    cwords = _sig_words(company)
    soup = BeautifulSoup(r.text, "html.parser")
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if "cdn.worldvectorlogo.com" not in src or not src.endswith(_SVG_EXT):
            continue
        cdn_slug = src.rsplit("/", 1)[-1].lower()
        if any(w in cdn_slug for w in cwords):
            return {"url": src, "source": "WorldVectorLogo"}
    return None


# ── SVG Strategy 8: Company website ──────────────────────────────────────────

def _company_domains(company):
    """Generate candidate domain names for a company.

    Returns a deduplicated list ordered so the most-likely real domains come
    first: maximal-stripped core brand, then the full condensed name, then
    individual-word-stripped variants, then hyphenated versions.
    """
    spaced = re.sub(r"'s?", '', company.lower())
    spaced = re.sub(r'\s+(inc|llc|corp|ltd|co|the)\s*$', '', spaced.strip())
    condensed = re.sub(r'[^a-z0-9]', '', spaced)

    _DROP = ('corporation', 'corp', 'native', 'realty', 'financial',
             'enterprises', 'services', 'solutions', 'group', 'holdings',
             'markets', 'market', 'industries', 'industry', 'systems',
             'technologies', 'technology', 'partners', 'associates',
             'properties', 'property', 'management',
             'grocery', 'super', 'supermarket', 'supermarkets', 'stores', 'store',
             'restaurant', 'restaurants', 'foods', 'food', 'beverage',
             'beverages', 'brewing', 'distilling',
             'hotel', 'hotels', 'resort', 'resorts', 'hospitality',
             'tire', 'tires', 'auto', 'automotive', 'motors',
             'bank', 'bancorp', 'bancshares', 'bankshares', 'insurance', 'mutual',
             'energy', 'power', 'electric', 'gas', 'oil', 'petroleum',
             'mining', 'resources',
             'global', 'worldwide', 'international', 'national', 'american',
             'media', 'communications', 'entertainment', 'studios',
             'healthcare', 'health', 'medical', 'pharmaceutical',
             'construction', 'building', 'supply', 'distribution',
             'ranch', 'farm',)

    # ── 1. Maximal strip (computed first so it lands near front of list) ──────
    # Iteratively strip all noise words until stable.
    # Catches multi-word noise like "Publix Super Markets" → "publix".
    max_spaced = spaced
    for _ in range(5):
        prev = max_spaced
        for drop in _DROP:
            max_spaced = re.sub(r'\b' + drop + r'\b', '', max_spaced)
        max_spaced = re.sub(r'\s+', ' ', max_spaced).strip()
        if max_spaced == prev:
            break
    max_stripped = re.sub(r'[^a-z0-9]', '', max_spaced)
    max_hyph     = re.sub(r'-+', '-', re.sub(r'[^a-z0-9-]', '', max_spaced)).strip('-')

    # ── 2. Hyphenated original (e.g. "chick-fil-a") ───────────────────────────
    hyphenated = re.sub(r'-+', '-', re.sub(r'[^a-z0-9-]', '', spaced)).strip('-')

    # ── 3. Build ordered list ─────────────────────────────────────────────────
    # Most-likely real domain first, full name second, variants after.
    # Allow 2-char max_stripped when the company contains a short uppercase
    # abbreviation like "GE Aerospace" → max_stripped = "ge" → ge.com / geaerospace.com
    _has_short_abbrev = any(len(w) >= 2 and w.upper() == w and w.isalpha()
                            for w in company.split())
    candidates = []
    _min_len = 2 if _has_short_abbrev else 3
    if max_stripped and len(max_stripped) >= _min_len:
        candidates.append(max_stripped)
    candidates.append(condensed)
    # For bank holding companies: also try the "bank" form of the domain.
    # e.g. "Zions Bancorp" → condensed "zionsbancorp" → also try "zionsbank"
    for _bc, _bk in (('bancorp', 'bank'), ('bancshares', 'bank'), ('bankshares', 'bank')):
        if _bc in condensed:
            _bk_dom = condensed.replace(_bc, _bk)
            if _bk_dom and _bk_dom not in candidates and len(_bk_dom) >= 3:
                candidates.insert(1, _bk_dom)   # try before bare condensed form
    if max_hyph and max_hyph not in candidates and len(max_hyph) >= 3:
        candidates.append(max_hyph)
    if hyphenated not in candidates and len(hyphenated) >= 3:
        candidates.append(hyphenated)

    # Individual single-word strips
    for drop in _DROP:
        s_spaced = re.sub(r'\s+', ' ', re.sub(r'\b' + drop + r'\b', '', spaced)).strip()
        s = re.sub(r'[^a-z0-9]', '', s_spaced)
        if s and s not in candidates and len(s) >= 3:
            candidates.append(s)
        s_h = re.sub(r'-+', '-', re.sub(r'[^a-z0-9-]', '', s_spaced)).strip('-')
        if s_h and s_h not in candidates and len(s_h) >= 3:
            candidates.append(s_h)

    return list(dict.fromkeys(candidates))


def _domain_confirms_company(domain_name, page_text, company):
    """
    Guard against hitting a DIFFERENT company that shares a partial name.
    Example: "Admiral Beverage" → domain "admiral" → admiral.com (insurance).
    If any sig_word is absent from the domain AND absent from the page text,
    we're on the wrong site — return False to skip this domain.
    """
    cwords = _sig_words(company)
    if not cwords:
        return True
    words_not_in_domain = {w for w in cwords if w not in domain_name}
    if not words_not_in_domain:
        return True  # all sig_words already baked into the domain name
    # At least one sig_word is missing from the domain — verify via page text.
    # Use full text: the first 4000 chars is usually <head> CSS/JS and the
    # meaningful keywords ("Financial", "Native Corporation", etc.) come later.
    page_lower = page_text.lower()
    return any(w in page_lower for w in words_not_in_domain)


def try_company_website_svg(company, **_):
    from urllib.parse import urlparse as _up
    company_slug = re.sub(r'[^a-z0-9]', '', company.lower())
    cwords = _sig_words(company)

    def _img_is_logo(img, final_url, require_company_id=True):
        """Return result dict if img is a logo SVG, else None."""
        src = img.get("src", "")
        sl  = src.lower()
        if not sl.endswith(_SVG_EXT):
            return None
        try:
            spath = _up(src).path.lower() if "://" in src else sl
        except Exception:
            spath = sl
        alt_cl = (img.get("alt", "") + " " + " ".join(img.get("class", []))).lower()
        has_logo = "logo" in spath or "logo" in alt_cl
        has_company = (company_slug in spath
                       or any(w in spath for w in cwords)
                       or any(w in alt_cl for w in cwords))
        if has_logo and (not require_company_id or has_company):
            full = src if src.startswith("http") else urljoin(final_url, src)
            return {"url": full, "source": "Company Website", "_reliable_source": True}
        return None

    def _svg_is_logo(svg):
        """Return result dict if inline SVG is a self-contained logo, else None."""
        attrs = (svg.get("id", "") + " " + " ".join(svg.get("class", []))).lower()
        if "logo" not in attrs:
            return None
        content = str(svg)
        # Reject empty wrappers like <svg class="logo"><use href="#..."/></svg> —
        # they have no embedded path data and render blank when served standalone.
        if not re.search(r'<(path|circle|rect|polygon|polyline|ellipse|line)\b', content):
            return None
        return {"url": None, "source": "Company Website",
                "_content": content, "_reliable_source": True}

    for name in _company_domains(company):
        url = f"https://www.{name}.com"
        r = _get(url, allow_redirects=True, timeout=5)
        if not r or r.status_code not in (200, 301, 302):
            continue
        if not _domain_confirms_company(name, r.text, company):
            continue
        final_url = r.url
        soup = BeautifulSoup(r.text, "html.parser")

        # Phase 1: header/nav only — company identity is implicit in the site header.
        for container in soup.find_all(['header', 'nav']):
            for img in container.find_all("img", src=True):
                res = _img_is_logo(img, final_url, require_company_id=False)
                if res:
                    return res
            for svg in container.find_all("svg"):
                res = _svg_is_logo(svg)
                if res:
                    return res

        # Phase 2: full page — require company identity in URL path or alt text
        # to avoid grabbing vendor/partner/product logos scattered around the page.
        for img in soup.find_all("img", src=True):
            res = _img_is_logo(img, final_url, require_company_id=True)
            if res:
                return res
        for svg in soup.find_all("svg"):
            res = _svg_is_logo(svg)
            if res:
                return res

    return None


# ── Raster Strategy 0: Wikipedia (API-only) ─────────────────────────────────
# Replaces infobox raster, wiki raster, AND pageimage in a single function.
# One combined API call per article; no HTML fetching.

def try_wikipedia_raster(company, category="", state=""):
    cwords = _sig_words(company)
    for query in _wiki_queries(company, category, state)[:1]:
        for article in _wiki_search(query, limit=1):
            time.sleep(0.15)
            imgs, pi = _wiki_article_data(article, cwords)

            # 1. Logo-named raster files with a company sig_word in filename
            for title in imgs:
                tl = title.lower()
                if not any(tl.endswith(ext) for ext in _RASTER_EXTS):
                    continue
                if _SKIP_FILES.search(title) or "logo" not in tl:
                    continue
                if cwords and not any(w in tl for w in cwords):
                    continue
                if not _disambig_ok(title, cwords):
                    continue
                if _title_extra_words(title, cwords) > 1:
                    continue
                url = _wiki_file_url(title)
                if url:
                    # Filename sig_word match already confirms identity
                    return {"url": url, "source": "Wikipedia",
                            "format": _ext_from_url(url), "_reliable_source": True}

            # 2. Pageimage fallback — Wikipedia's chosen representative image.
            # Require "logo" in the filename AND a company word in the URL.
            # Without the "logo" check we risk accepting store/founder photos
            # whose URLs happen to contain the company name.
            if pi and not _SKIP_FILES.search(pi.lower()):
                pi_l = pi.lower().split("?")[0]
                if not pi_l.endswith(".svg"):   # SVG handled in SVG phase
                    pi_file = pi_l.rsplit("/", 1)[-1]   # filename only
                    uw = _url_words(cwords)
                    if "logo" in pi_file and uw and any(w in pi.lower() for w in uw):
                        return {"url": pi, "source": "Wikipedia",
                                "format": _ext_from_url(pi),
                                "_reliable_source": True}
    return None


# ── Raster Strategy 1: Wikimedia Commons (PNG/JPG logos) ─────────────────────

def try_wikimedia_commons_raster(company, category="", state=""):
    cwords = _sig_words(company)
    for query in [f"{company} logo"]:   # one query only
        if not query:
            continue
        r = _get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "list": "search",
            "srsearch": query, "srnamespace": "6",
            "srlimit": "10", "format": "json",
        })
        if not r or r.status_code != 200:
            continue
        for item in r.json().get("query", {}).get("search", []):
            title = item["title"]
            tl = title.lower()
            if not any(tl.endswith(ext) for ext in _RASTER_EXTS):
                continue
            if _SKIP_FILES.search(title) or "logo" not in tl:
                continue
            if not _disambig_ok(title, cwords):
                continue
            if _title_extra_words(title, cwords) > 1:
                continue
            time.sleep(0.2)
            url = _commons_file_url(title)
            if url:
                return {"url": url, "source": "Wikimedia Commons",
                        "format": _ext_from_url(url), "_reliable_source": True}
    return None


# ── Raster Strategy 2: Clearbit (PNG from domain) ────────────────────────────

def try_clearbit(company, **_):
    """Fast domain-based lookup — try all domain variants (CDN requests are cheap)."""
    for name in _company_domains(company)[:4]:   # condensed + hyphenated + stripped
        r = _get(f"https://logo.clearbit.com/{name}.com", timeout=4)
        if r and r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
            ct = r.headers.get("Content-Type", "")
            fmt = "jpg" if "jpeg" in ct else "png"
            return {"url": f"https://logo.clearbit.com/{name}.com",
                    "source": "Clearbit", "format": fmt, "_reliable_source": True}
    return None


# ── Fallback: Facebook Graph API (public, no auth) ───────────────────────────
# Facebook's Graph API /picture endpoint is publicly accessible and returns
# the company's profile picture — usually their logo.

def try_facebook_logo(company, **_):
    """Fetch company profile picture from Facebook's public Graph API."""
    cwords = _sig_words(company)
    # Build slug variants from company name + domain list
    slugs = []
    for s in [_slug_plain(company), _slug(company).replace('-', '')]:
        if s and s not in slugs:
            slugs.append(s)
    for d in _company_domains(company)[:3]:
        if d not in slugs:
            slugs.append(d)

    for slug in slugs:
        r = _get(
            f"https://graph.facebook.com/{slug}/picture",
            params={"type": "normal", "redirect": "false"},
            timeout=5,
        )
        if not r or r.status_code != 200:
            continue
        try:
            data = r.json().get("data", {})
        except Exception:
            continue
        if data.get("is_silhouette", True):
            continue        # default grey icon — not a real logo
        pic_url = data.get("url", "")
        if pic_url:
            return {"url": pic_url, "source": "Facebook",
                    "format": _ext_from_url(pic_url) or "jpg",
                    "_reliable_source": True}
    return None


# ── Image search: Google Custom Search → DuckDuckGo → Bing ───────────────────

# Tracks when the Google daily quota (100 req/day free) is exhausted.
# Resets on server restart; DDG takes over automatically for the rest of the day.
_google_quota_hit = False


def try_google_images(company, state="", **_):
    """Google Custom Search API — best quality, 100 free queries/day."""
    global _google_quota_hit
    if _google_quota_hit:
        return None

    try:
        from config import GOOGLE_API_KEY, GOOGLE_CX
    except ImportError:
        GOOGLE_API_KEY, GOOGLE_CX = "", ""

    key = GOOGLE_API_KEY or os.environ.get("GOOGLE_API_KEY", "")
    cx  = GOOGLE_CX      or os.environ.get("GOOGLE_CX", "")
    if not key or not cx:
        return None

    query = f"{company} {state} logo".strip() if state else f"{company} logo"
    r = _get("https://www.googleapis.com/customsearch/v1", params={
        "key": key, "cx": cx, "q": query,
        "searchType": "image", "num": 10, "safe": "active",
    }, timeout=10)

    if not r:
        return None
    if r.status_code in (429, 403):
        try:
            errs = r.json().get("error", {}).get("errors", [{}])
            if any(e.get("reason") in ("rateLimitExceeded", "dailyLimitExceeded",
                                       "userRateLimitExceeded") for e in errs):
                _google_quota_hit = True
        except Exception:
            pass
        return None
    if r.status_code != 200:
        return None

    try:
        items = r.json().get("items", [])
    except Exception:
        return None

    for item in items:
        url = item.get("link", "")
        if not url:
            continue
        ul = url.lower().split("?")[0]
        if not any(ul.endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp", ".svg")):
            continue
        if _STOCK_SITES.search(url):
            continue
        return {"url": url, "source": "Google Images",
                "format": _ext_from_url(url), "_search_result": True}
    return None


# Rate-limit DDG similarly to Bing.
_ddg_lock   = _threading.Lock()
_ddg_last_t = [0.0]

def _ddg_rate_limit():
    with _ddg_lock:
        gap = time.time() - _ddg_last_t[0]
        if gap < 1.5:
            time.sleep(1.5 - gap)
        _ddg_last_t[0] = time.time()


def try_duckduckgo_images(company, state="", **_):
    """DuckDuckGo image search — no API key required; used when Google quota runs out."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return None

    query = f"{company} {state} logo".strip() if state else f"{company} logo"
    _ddg_rate_limit()
    try:
        results = list(DDGS().images(query, max_results=15))
    except Exception:
        return None

    for item in results:
        url = item.get("image", "")
        if not url:
            continue
        ul = url.lower().split("?")[0]
        if not any(ul.endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp", ".svg")):
            continue
        if _STOCK_SITES.search(url):
            continue
        return {"url": url, "source": "DuckDuckGo Images",
                "format": _ext_from_url(url), "_search_result": True}
    return None


# ── Last-resort: Bing image search ───────────────────────────────────────────
# Only reached when all dedicated strategies AND Facebook have returned nothing.
# Bing embeds full-resolution image URLs in its HTML as HTML-entity-encoded JSON.

_BING_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}
_STOCK_SITES = re.compile(
    r'(wikimedia|wikipedia|shutterstock|istockphoto|gettyimages|alamy'
    r'|depositphotos|dreamstime|123rf|adobe\.com/images)', re.I)

# Rate-limit Bing: with 5 parallel workers each hitting Bing simultaneously,
# Bing quickly responds with bot-detection blocks (empty murl lists).
# Serialise all Bing requests to at most one per second.
_bing_lock   = _threading.Lock()
_bing_last_t = [0.0]

def _bing_rate_limit():
    with _bing_lock:
        gap = time.time() - _bing_last_t[0]
        if gap < 1.2:
            time.sleep(1.2 - gap)
        _bing_last_t[0] = time.time()


def _bing_fetch(query):
    """Fetch Bing image search HTML and extract murl list; returns [] on failure."""
    import html as _html
    _bing_rate_limit()
    r = _get(
        f"https://www.bing.com/images/search?q={quote(query)}&form=HDRSC2",
        headers=_BING_BROWSER_HEADERS,
        timeout=12,
    )
    if not r or r.status_code != 200:
        return []
    return re.findall(r'"murl":"(https?://[^"\\]+)"', _html.unescape(r.text))


def try_bing_image(company, state="", **_):
    """Bing image search — three-pass URL quality check, two query attempts."""
    from urllib.parse import urlparse as _urlparse

    # Try with state first; if Bing returns nothing, fall back to name-only query.
    queries = []
    if state:
        queries.append(f"{company} {state} logo")
    queries.append(f"{company} logo")

    murls = []
    for q in queries:
        murls = _bing_fetch(q)
        if murls:
            break

    if not murls:
        return None

    cwords = _sig_words(company)

    # The full condensed slug (e.g. "llbean", "hyvee") and max-stripped domain
    # (e.g. "wegmans", "admiral") both appear in the PATH of official logo URLs.
    company_slug = re.sub(r'[^a-z0-9]', '', company.lower())
    domains = _company_domains(company)
    slug_set = {s for s in ({company_slug} | ({domains[0]} if domains else set()))
                if len(s) >= 4}

    def _valid(url):
        ul = url.lower().split("?")[0]
        return (any(ul.endswith(e) for e in (".png", ".jpg", ".jpeg", ".webp"))
                and not _STOCK_SITES.search(url))

    def _url_path(url):
        try:
            return _urlparse(url).path.lower()
        except Exception:
            return url.lower()

    def _ret(url):
        return {"url": url, "source": "Web Image Search",
                "format": _ext_from_url(url), "_search_result": True}

    # ── Pass 1: strong — "logo" in URL PATH, OR full company slug in PATH ─────
    # Checking only the PATH (not the hostname) prevents matching every image
    # on llbean.com or hyvee.com just because the domain name has a sig-word.
    for url in murls:
        if not _valid(url):
            continue
        path = _url_path(url)
        if "logo" in path:
            return _ret(url)
        if any(s in path for s in slug_set):
            return _ret(url)

    # ── Pass 2: medium — 2+ sig_words in URL path ────────────────────────────
    if len(cwords) >= 2:
        for url in murls:
            if not _valid(url):
                continue
            path = _url_path(url)
            if sum(1 for w in cwords if w in path) >= 2:
                return _ret(url)

    # ── Pass 3: last resort — company in URL AND "logo" in URL ───────────────
    # Requiring "logo" in the URL prevents lifestyle/product photos that happen
    # to come from the company's own domain (e.g. llbean.com/gifts_fathersday.jpg
    # passes the slug check because "llbean" is in the hostname).
    for url in murls:
        if not _valid(url):
            continue
        ul = url.lower()
        if "logo" not in ul:
            continue
        if any(s in ul for s in slug_set if len(s) >= 4):
            return _ret(url)
        if cwords and any(w in ul for w in cwords if len(w) >= 5):
            return _ret(url)
    return None


# ── Raster Strategy 5: Company website (any logo image) ──────────────────────

def try_company_website_raster(company, **_):
    from urllib.parse import urlparse as _up
    cwords = _sig_words(company)
    company_slug = re.sub(r'[^a-z0-9]', '', company.lower())

    def _img_is_logo(img, final_url, require_company_id=True):
        src = img.get("src", "")
        sl = src.lower()
        if not any(sl.endswith(ext) for ext in _RASTER_EXTS):
            return None
        # Skip obvious white/reversed color variants — invisible on white background
        if re.search(r'[-_]white[-_.]|[-_]wht[-_.]|[-_]reversed[-_.]', sl):
            return None
        try:
            spath = _up(src).path.lower() if "://" in src else sl
        except Exception:
            spath = sl
        alt_cl = (img.get("alt", "") + " " + " ".join(img.get("class", []))).lower()
        has_logo = "logo" in spath or "logo" in alt_cl
        has_company = (company_slug in spath
                       or any(w in spath for w in cwords)
                       or any(w in alt_cl for w in cwords))
        if has_logo and (not require_company_id or has_company):
            full = src if src.startswith("http") else urljoin(final_url, src)
            return {"url": full, "source": "Company Website",
                    "format": _ext_from_url(full), "_reliable_source": True}
        return None

    for name in _company_domains(company):
        url = f"https://www.{name}.com"
        r = _get(url, allow_redirects=True, timeout=5)
        if not r or r.status_code not in (200, 301, 302):
            continue
        if not _domain_confirms_company(name, r.text, company):
            continue
        final_url = r.url
        soup = BeautifulSoup(r.text, "html.parser")

        # Phase 1: header/nav — company identity is implicit in the site header
        for container in soup.find_all(['header', 'nav']):
            for img in container.find_all("img", src=True):
                res = _img_is_logo(img, final_url, require_company_id=False)
                if res:
                    return res

        # Phase 2: full page — require company identity to avoid product/vendor logos
        for img in soup.find_all("img", src=True):
            res = _img_is_logo(img, final_url, require_company_id=True)
            if res:
                return res

        # Phase 3: og:image — only if URL PATH contains "logo" and company identity
        og = (soup.find("meta", property="og:image")
              or soup.find("meta", attrs={"name": "og:image"}))
        if og and og.get("content"):
            og_url = og["content"].strip()
            try:
                og_path = _up(og_url).path.lower()
            except Exception:
                og_path = og_url.lower()
            if "logo" in og_path and any(w in og_path for w in cwords):
                if any(og_url.lower().split("?")[0].endswith(e) for e in _RASTER_EXTS):
                    full = og_url if og_url.startswith("http") else urljoin(final_url, og_url)
                    return {"url": full, "source": "Company Website",
                            "format": _ext_from_url(full), "_reliable_source": True}
    return None


# ── Logo text validation ─────────────────────────────────────────────────────

# Generic industry-descriptor words that rarely appear in logo file URLs.
# Requiring them in the URL check produces false "unverified" rejections for
# companies like "Michelin Tire", "Regions Financial", "Wegmans Food Markets".
_GENERIC_SFX = frozenset({
    'tire', 'tires', 'financial', 'foods', 'food', 'beverage', 'beverages',
    'bank', 'banking', 'bancorp', 'bancshares', 'bankshares',
    'auto', 'automotive', 'motors', 'energy', 'health', 'healthcare',
    'media', 'national', 'global', 'international', 'american', 'group',
    'services', 'service', 'solutions', 'markets', 'market', 'stores', 'store',
    'resorts', 'resort', 'hospitality', 'insurance', 'mutual',
    'properties', 'realty', 'enterprises', 'corporation', 'native',
})


def _url_words(words):
    """Strip generic suffixes so the URL check focuses on the brand name."""
    filtered = words - _GENERIC_SFX
    return filtered if filtered else words


# Words that are OK to appear in logo file titles without being "extra" brand words.
_TITLE_OK = frozenset({
    'logo', 'file', 'svg', 'png', 'jpg', 'jpeg', 'webp', 'the', 'of', 'and', 'a',
    'inc', 'ltd', 'llc', 'co', 'corporation', 'incorporated', 'limited',
    'bancorporation', 'national', 'american', 'international', 'group', 'holdings',
    'realty', 'financial', 'enterprises', 'services', 'solutions', 'markets',
    'stores', 'foods', 'beverage', 'beverages', 'resorts', 'properties',
    'automotive', 'bancorp', 'bancshares', 'bankshares', 'insurance',
})


def _title_extra_words(title, cwords):
    """
    Count significant words in a logo file title that don't belong to the company.
    Used to reject sub-brand/product logos (e.g. "Michelin PAX System logo" when
    we want the main "Michelin" brand logo).
    Returns 0 for titles that match the company name well.
    """
    twords = set(re.sub(r'[^a-z]', ' ', title.lower()).split())
    twords -= _TITLE_OK
    twords = {w for w in twords if len(w) > 2 and not w.isdigit()}
    return len(twords - cwords)

def _validate_logo(file_path, url, company, fmt):
    """
    Returns one of:
      'verified'   – company name found in RENDERED text elements (name visible
                     in the actual graphic — the preferred result)
      'meta_only'  – name found only in <title>/<desc>/URL/filename (icon/symbol
                     logo; correct file but wordmark not present)
      'unverified' – no text at all and name not in URL/filename; pure graphic,
                     can't confirm identity (accept as last resort)
      'mismatch'   – rendered text IS present but does not match the company;
                     caller should delete and try the next source
    """
    words = _sig_words(company)
    if fmt == "svg":
        return _validate_svg(file_path, url, words)
    else:
        return _validate_raster(file_path, url, words)


def _validate_svg(file_path, url, words):
    import xml.etree.ElementTree as ET

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception:
        return "unverified"

    # Tags whose content is RENDERED (visible) in the graphic
    VISIBLE_TAGS  = {"text", "tspan", "textpath", "flowroot", "flowpara", "flowspan"}
    # Tags that are metadata / accessibility labels only
    META_TAGS     = {"title", "desc"}

    visible_parts = []
    meta_parts    = []
    has_visible   = False

    try:
        root = ET.fromstring(raw)
        for el in root.iter():
            tag = (el.tag.split("}")[-1] if "}" in el.tag else el.tag).lower()
            for part in (el.text, el.tail):
                if part and part.strip():
                    if tag in VISIBLE_TAGS:
                        has_visible = True
                        visible_parts.append(part.strip().lower())
                    elif tag in META_TAGS:
                        meta_parts.append(part.strip().lower())
    except ET.ParseError:
        pass

    vis = " ".join(visible_parts)
    if any(w in vis for w in words):
        return "verified"      # ← name rendered visibly in the graphic ✓

    if has_visible:
        return "mismatch"      # ← visible text exists but it's the wrong company

    # No rendered text — check metadata + SOURCE URL for identity confirmation.
    # We deliberately do NOT check the local filename: it's set by us to the
    # company name, so it always "matches" and would hide wrong-company images.
    meta    = " ".join(meta_parts)
    src_url = (url or "").lower()

    # Use brand-only words for URL check: strip generic industry suffixes like
    # "Tire", "Financial", "Foods" — they rarely appear in logo URLs, so
    # requiring them causes correct logos to fall through to "unverified".
    uw = _url_words(words)
    combined_meta_url = meta + " " + src_url
    if uw and all(w in combined_meta_url for w in uw):
        return "meta_only"

    # Raw string scan: catches inline styles, comments, embedded metadata, etc.
    if uw and all(w in raw.lower() for w in uw):
        return "meta_only"

    return "unverified"


def _validate_raster(file_path, url, words):
    # ── Check SOURCE URL — strip generic suffixes before requiring ALL words ──
    src_url = (url or "").lower()
    uw = _url_words(words)
    if uw and all(w in src_url for w in uw):
        return "meta_only"

    # ── OCR via pytesseract (optional, requires Tesseract binary) ───────────
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(file_path).convert("RGB")
        w, h = img.size
        if max(w, h) < 300:
            scale = 300 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        ocr_text = pytesseract.image_to_string(img).lower()
        if any(w in ocr_text for w in words):
            return "verified"
        if ocr_text.strip():
            return "mismatch"
    except Exception:
        pass  # Tesseract not installed — fall through

    return "unverified"


# ── File helpers ──────────────────────────────────────────────────────────────

def _ext_from_url(url):
    ul = url.lower().split("?")[0]
    for ext in ("svg", "png", "jpg", "jpeg", "webp"):
        if ul.endswith(f".{ext}"):
            return "jpg" if ext == "jpeg" else ext
    return "png"  # assume PNG if unknown


def download_svg(url, filename, content=None):
    path = os.path.join(LOGOS_DIR, filename)
    if content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    r = _get(url)
    if not r or r.status_code != 200:
        return None
    text = r.text
    if len(text) < 80:   # too small to be a real SVG
        return None
    ct = r.headers.get("Content-Type", "")
    if "svg" in ct or text.strip().startswith("<svg") or (
        "<?xml" in text[:100] and "<svg" in text[:500]
    ):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    return None


def download_raster(url, filename):
    r = _get(url)
    if not r or r.status_code != 200:
        return None
    ct = r.headers.get("Content-Type", "")
    if "image" not in ct and not any(url.lower().endswith(e) for e in _RASTER_EXTS):
        return None
    if len(r.content) < 300:   # too small — blank/error placeholder
        return None
    path = os.path.join(LOGOS_DIR, filename)
    with open(path, "wb") as f:
        f.write(r.content)
    return path


# ── Main entry point ──────────────────────────────────────────────────────────

SVG_STRATEGIES = [
    try_vectorlogo_wordmark,    # 1. VectorLogo.zone horizontal lockup (wordmark)
    try_simple_icons,           # 2. Simple Icons CDN
    try_wikipedia_svg,          # 3. Wikipedia logo-named files (API-only)
    try_wikimedia_commons_svg,  # 4. Wikimedia Commons search
    try_company_website_svg,    # 5. company homepage (before WorldVectorLogo HTML scrape)
    try_worldvectorlogo,        # 6. WorldVectorLogo CDN (slow HTML parse, last SVG resort)
    try_vectorlogo_icon,        # 7. VectorLogo.zone icon-only fallback
]

RASTER_STRATEGIES = [
    try_company_website_raster, # 0. company homepage + og:image
    try_wikipedia_raster,       # 1. Wikipedia logo-named rasters (API-only)
    try_wikimedia_commons_raster,  # 2. Commons rasters
    try_facebook_logo,          # 3. Facebook Graph API public profile picture
    # Note: Clearbit (logo.clearbit.com) removed — DNS no longer resolves (HubSpot acquired)
]


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def scrape_logo(company, category="", state="", exclude_urls=None):
    """
    exclude_urls: iterable of URL strings (or "(inline)") already shown to the user.
    When set, any strategy result whose URL is in this set is skipped so the scraper
    returns the next-best candidate.  Used by the "Try Another" retry cycling UI.
    """
    _excl = set(exclude_urls or [])
    kwargs  = {"category": category, "state": state}
    sfn     = safe_filename(company)
    base    = {
        "company": company,
        "status":  "not_found",
        "source":  None,
        "file":    None,
        "url":     None,
        "format":  None,
    }

    # ── Phase 1: SVG ─────────────────────────────────────────────────────────
    # Priority order: "verified" (name rendered in graphic) > "meta_only"
    # (correct icon/symbol logo) > "unverified" (pure graphic).
    # We use per-attempt temp filenames so fallback files are never overwritten.

    _PRIO = {"verified": 3, "meta_only": 2, "unverified": 1, "mismatch": 0}
    best_svg_result = None   # result dict of the best non-verified SVG so far
    best_svg_path   = None   # path on disk for that fallback
    best_svg_prio   = 0

    # Strategies that are "slow" (web scrapes / multi-request chains).
    # Once we already have a confirmed meta_only SVG we stop before these.
    _SLOW_SVG = {try_company_website_svg, try_worldvectorlogo}

    for idx, fn in enumerate(SVG_STRATEGIES):
        # Short-circuit: if we already have a confirmed meta_only SVG, skip
        # the slow web-scraping strategies — they're unlikely to do better.
        if best_svg_prio >= 2 and fn in _SLOW_SVG:
            continue

        # Website strategies may iterate several domains — give them more time
        _timeout = 20 if fn in _SLOW_SVG else 10
        info = _call_with_timeout(fn, company, kwargs, seconds=_timeout)
        if not info:
            continue
        # Skip URLs already shown to the user (retry cycling)
        _result_url = info.get("url") or "(inline)"
        if _excl and _result_url in _excl:
            continue

        tmp_name = f"{sfn}_t{idx}.svg"
        path = download_svg(info.get("url"), tmp_name, content=info.get("_content"))
        if not path:
            continue

        # _wordmark flag = VectorLogo.zone ar21 horizontal lockup — treat as verified
        # even when text is path-converted (it IS the brand wordmark by definition)
        if info.get("_wordmark"):
            verification = "verified"
        else:
            verification = _validate_logo(path, info.get("url", ""), company, "svg")

        if verification == "verified":
            # Best possible — rename to canonical name, clean up any fallback
            if best_svg_path:
                _remove(best_svg_path)
            final_name = sfn + ".svg"
            final_path = os.path.join(LOGOS_DIR, final_name)
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
                os.rename(path, final_path)
            except OSError:
                final_name = tmp_name   # keep temp name if rename fails
            return {**base, "status": "found", "source": info["source"],
                    "file": final_name, "url": info.get("url") or "(inline)",
                    "format": "svg", "verified": "verified"}

        elif verification == "mismatch":
            _remove(path)

        else:   # "meta_only" or "unverified"
            # Reject "unverified" SVGs from non-reliable sources (Wikipedia,
            # Commons, DDG…). Without URL/metadata confirmation we can't trust
            # that the graphic belongs to this company.
            if verification == "unverified" and not info.get("_reliable_source"):
                _remove(path)
                continue
            # Reliable-source SVG (company website) — domain confirms identity
            if verification == "unverified" and info.get("_reliable_source"):
                verification = "meta_only"
            prio = _PRIO.get(verification, 0)
            if prio > best_svg_prio:
                if best_svg_path:
                    _remove(best_svg_path)
                best_svg_prio   = prio
                best_svg_path   = path
                best_svg_result = {**base, "status": "found", "source": info["source"],
                                   "file": tmp_name, "url": info.get("url") or "(inline)",
                                   "format": "svg", "verified": verification}
            else:
                _remove(path)

    # Use best SVG fallback if any
    if best_svg_result:
        final_name = sfn + ".svg"
        final_path = os.path.join(LOGOS_DIR, final_name)
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(best_svg_path, final_path)
            best_svg_result = {**best_svg_result, "file": final_name}
        except OSError:
            pass  # keep temp filename if rename fails
        return best_svg_result

    # ── Phase 2: Raster fallback ──────────────────────────────────────────────
    _SLOW_RASTER = {try_company_website_raster}
    for fn in RASTER_STRATEGIES:
        _timeout = 20 if fn in _SLOW_RASTER else 10
        info = _call_with_timeout(fn, company, kwargs, seconds=_timeout)
        if not info or not info.get("url"):
            continue
        if _excl and info["url"] in _excl:
            continue
        fmt      = info.get("format", "png")
        filename = sfn + "." + fmt
        path     = download_raster(info["url"], filename)
        if path:
            verification = _validate_logo(path, info.get("url", ""), company, fmt)
            if verification == "mismatch":
                _remove(path)
                continue
            if verification == "unverified" and not info.get("_reliable_source"):
                _remove(path)
                continue
            if verification == "unverified" and info.get("_reliable_source"):
                verification = "meta_only"
            return {**base, "status": "found", "source": info["source"],
                    "file": filename, "url": info["url"],
                    "format": fmt, "verified": verification}

    # ── Phase 3: Image search — Google → DuckDuckGo → Bing ──────────────────
    # Google is tried first (best quality). When its daily quota runs out the
    # flag _google_quota_hit is set and DDG takes over automatically.
    # Bing remains the last resort if both are unavailable or return nothing.
    for search_fn in [try_google_images, try_duckduckgo_images, try_bing_image]:
        info = _call_with_timeout(search_fn, company, kwargs, seconds=20)
        if not info or not info.get("url") or info["url"] in _excl:
            continue
        url = info["url"]
        fmt = _ext_from_url(url)
        if fmt == "svg":
            path = download_svg(url, sfn + ".svg")
            if path:
                verification = _validate_logo(path, url, company, "svg")
                if verification == "mismatch":
                    _remove(path)
                    continue
                return {**base, "status": "found", "source": info["source"],
                        "file": sfn + ".svg", "url": url,
                        "format": "svg", "verified": verification}
        else:
            filename = sfn + "." + fmt
            path     = download_raster(url, filename)
            if path:
                return {**base, "status": "found", "source": info["source"],
                        "file": filename, "url": url,
                        "format": fmt, "verified": "unverified"}

    return base
