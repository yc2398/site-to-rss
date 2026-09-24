"""
Extensible RSS feed generator with full content extraction.
Reads sources.yml, checks for new content, generates RSS 2.0 (and Atom) feeds.
"""

import hashlib
import json
import os
import re
import sys
import traceback
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring, parse

import yaml
from lxml import html as lxml_html
from lxml import etree
from lxml.cssselect import CSSSelector
import markdown


# ─── Constants ─────────────────────────────────────────────────────────────

SOURCES_FILE = "sources.yml"
STATE_FILE = "state.json"
FEED_FILE = "docs/feed.xml"
# The canonical `.xml` file carries RSS 2.0 — that is what most readers
# actually render (the Atom `<summary>` is ignored by a fair number of them).
# The Atom flavour is still written alongside it for anyone who prefers it.
ATOM_FEED_FILE = "docs/feed.atom.xml"
INDEX_TEMPLATE = "templates/index_template.html"
MAX_FEED_ITEMS = 100
REQUEST_TIMEOUT = 30

# Output flavours. `rss` writes <name>.xml, `atom` writes <name>.atom.xml.
DEFAULT_FORMATS = ["rss", "atom"]
KNOWN_FORMATS = ("rss", "atom")

DC_NS = "http://purl.org/dc/elements/1.1/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

# Default pattern for locating a DOI inside an arbitrary URL / text.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^/?#\s\"'<>]+")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%B, %Y",
    "%b, %Y",
]


def _now() -> str:
    """Current UTC time as an Atom-compatible timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(raw: str) -> str:
    """Collapse whitespace so extracted values read like normal prose."""
    return " ".join((raw or "").split())


def _parse_date(raw: str) -> str:
    """Best-effort date parsing. Returns '' when the value is unusable."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return ""


# ─── Date conversion between Atom (ISO 8601) and RSS 2.0 (RFC 822) ──────────
#
# Both directions are spelled out by hand instead of going through strftime /
# strptime, because %a and %b follow the process locale: on a machine with a
# non-English locale the same code would happily emit or choke on localised
# day/month names. Feed dates have to be English regardless of where the
# workflow happens to run.

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_MONTH_NUM = {name.lower(): i for i, name in enumerate(_MONTHS, start=1)}
_RFC822_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def _parse_iso(raw: str):
    """ISO 8601 -> aware datetime, or None when the value is unusable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    iso = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rfc822(raw: str) -> str:
    """Format a stored timestamp the way RSS 2.0 `<pubDate>` requires."""
    dt = _parse_iso(raw)
    if dt is None:
        return ""
    dt = dt.astimezone(timezone.utc)
    return "{}, {:02d} {} {} {:02d}:{:02d}:{:02d} +0000".format(
        _DAYS[dt.weekday()], dt.day, _MONTHS[dt.month - 1], dt.year,
        dt.hour, dt.minute, dt.second,
    )


def _iso_from_rfc822(raw: str) -> str:
    """Parse an RSS 2.0 `<pubDate>` back into the ISO form used internally."""
    match = _RFC822_RE.search(raw or "")
    if not match:
        return ""
    month = _MONTH_NUM.get(match.group(2)[:3].lower())
    if not month:
        return ""
    try:
        dt = datetime(
            int(match.group(3)), month, int(match.group(1)),
            int(match.group(4) or 0), int(match.group(5) or 0),
            int(match.group(6) or 0), tzinfo=timezone.utc,
        )
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dedupe_items(items: list) -> list:
    """Drop repeated entries, keeping the first occurrence of each id."""
    seen = set()
    result = []
    for item in items:
        key = item.get("id") or item.get("link") or item.get("title")
        if not key:
            result.append(item)
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


# ─── HTTP Helpers ──────────────────────────────────────────────────────────


def http_get(url: str, method: str = "GET") -> tuple:
    """Make an HTTP request. Returns (status_code, body)."""
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header(
        "Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    req.add_header("Accept-Language", "en-US,en;q=0.5")
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        if method == "GET":
            body = resp.read().decode("utf-8", errors="ignore")
        else:
            body = ""
        return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def http_get_json(url: str) -> dict:
    """GET a JSON document. Returns {} on any failure (never raises)."""
    status, body = http_get(url)
    if status != 200 or not body:
        return {}
    try:
        data = json.loads(body)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ─── Abstract / metadata enrichment ────────────────────────────────────────
#
# Listing pages (journal TOCs, news indexes) usually carry only a title, a
# link and maybe an author. The abstract lives on the detail page — which some
# publishers put behind a bot challenge (sage.cnpereading.com serves a
# SafeLine slider captcha for its /doi/ pages). Rather than fight the WAF we
# resolve the item through its DOI in a scholarly metadata API, which returns
# the very same abstract as clean text.


def _strip_markup(raw: str) -> str:
    """Drop JATS/HTML tags so a Crossref abstract becomes plain prose."""
    text = raw or ""
    if "<" not in text:
        return _clean_text(text)
    try:
        return _clean_text(lxml_html.fromstring(text).text_content())
    except Exception:
        return _clean_text(re.sub(r"<[^>]+>", " ", text))


def _extract_doi(text: str, pattern: str = "") -> str:
    """Pull the first DOI out of a URL or free text."""
    if not text:
        return ""
    rx = re.compile(pattern) if pattern else DOI_PATTERN
    match = rx.search(text)
    return match.group(0).rstrip(".,;") if match else ""


def _crossref_abstract(doi: str) -> str:
    data = http_get_json(
        "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    )
    message = data.get("message") or {}
    return _strip_markup(message.get("abstract") or "")


def _openalex_abstract(doi: str) -> str:
    data = http_get_json(
        "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="")
    )
    index = data.get("abstract_inverted_index")
    if not isinstance(index, dict) or not index:
        return ""
    # OpenAlex ships the abstract as {word: [positions]} to save space.
    positions = {}
    for word, places in index.items():
        for place in places:
            positions[place] = word
    return " ".join(positions[i] for i in sorted(positions))


ABSTRACT_PROVIDERS = {
    "crossref": _crossref_abstract,
    "openalex": _openalex_abstract,
}


def enrich_items(items: list, enrich_config: dict, source_name: str = "") -> int:
    """
    Fill missing `summary` values by looking each item up by DOI.

    Items that already have a summary are left alone, so a listing page that
    does carry an abstract wins over the API. Returns how many were enriched.
    """
    if not enrich_config or not items:
        return 0

    def as_list(value):
        if not value:
            return []
        return [value] if isinstance(value, str) else list(value)

    providers = [
        name
        for name in as_list(enrich_config.get("provider"))
        + as_list(enrich_config.get("fallback"))
        if name in ABSTRACT_PROVIDERS
    ]
    if not providers:
        print(f"    ⚠️  enrich: unknown provider(s); use one of "
              f"{sorted(ABSTRACT_PROVIDERS)}")
        return 0

    key_field = enrich_config.get("key", "link")
    pattern = enrich_config.get("doi_regex", "")
    max_items = int(enrich_config.get("max_items", 0) or 0)
    max_chars = int(enrich_config.get("max_chars", 0) or 0)
    label = f"  [{source_name}] " if source_name else "    "

    filled = 0
    for item in items:
        if max_items and filled >= max_items:
            break
        if item.get("summary"):
            continue
        doi = _extract_doi(str(item.get(key_field, "")), pattern)
        if not doi:
            continue

        for name in providers:
            try:
                abstract = ABSTRACT_PROVIDERS[name](doi)
            except Exception as e:  # never let enrichment kill the run
                print(f"{label}⚠️  enrich {doi} via {name} failed: {e}")
                continue
            if not abstract:
                continue
            if max_chars and len(abstract) > max_chars:
                abstract = abstract[:max_chars].rstrip() + "…"
            item["summary"] = abstract
            filled += 1
            print(f"{label}📄 abstract via {name} ({len(abstract)} chars): {doi}")
            break
        else:
            print(f"{label}⚠️  no abstract available for {doi}")

    return filled


def url_exists(url: str) -> bool:
    """Check if a URL is reachable with a 2xx status."""
    # Try HEAD first (faster)
    status, _ = http_get(url, method="HEAD")
    if 200 <= status < 400:
        return True
    # Some servers reject HEAD, fall back to GET
    status, _ = http_get(url, method="GET")
    return 200 <= status < 400


# ─── Content Extraction ───────────────────────────────────────────────────


def extract_content(
    page_url: str, content_config: dict, template_vars: dict = None
) -> str:
    """
    Fetch a page and extract content based on the config.
    Returns an HTML string to embed in the feed.
    """
    if not content_config:
        return ""

    content_type = content_config.get("type", "html")
    template_vars = template_vars or {}

    # Determine URL to fetch content from
    fetch_url = content_config.get("fetch_url", page_url)
    if template_vars:
        fetch_url = fetch_url.format(**template_vars)

    print(f"    📥 Fetching content: {fetch_url}")
    status, body = http_get(fetch_url)

    if status < 200 or status >= 400 or not body:
        print(f"    ⚠️  Failed to fetch content (HTTP {status})")
        return ""

    # Route to appropriate extractor
    if content_type == "markdown":
        base_url = content_config.get("base_url", "")
        return _convert_markdown(body, base_url)

    if content_type == "release_body":
        return _convert_markdown(body, "")

    # HTML extraction
    if "xpath" in content_config:
        return _extract_by_xpath(body, content_config)

    if "css" in content_config:
        return _extract_by_css(body, content_config)

    # Fallback: return body as-is (truncated)
    return body[:50000]


def _extract_by_xpath(html_str: str, config: dict) -> str:
    """Extract content from HTML using an XPath expression."""
    try:
        tree = lxml_html.fromstring(html_str)
    except Exception as e:
        print(f"    ⚠️  HTML parse error: {e}")
        return ""

    xpath_expr = config["xpath"]
    elements = tree.xpath(xpath_expr)

    if not elements:
        print(f"    ⚠️  XPath matched nothing: {xpath_expr}")
        return ""

    element = elements[0]
    _remove_elements(element, config.get("remove", []))

    content_html = etree.tostring(element, encoding="unicode", method="html")
    base_url = config.get("base_url", "")
    if base_url:
        content_html = _fix_relative_urls(content_html, base_url)

    return content_html


def _extract_by_css(html_str: str, config: dict) -> str:
    """Extract content from HTML using a CSS selector."""
    try:
        tree = lxml_html.fromstring(html_str)
    except Exception as e:
        print(f"    ⚠️  HTML parse error: {e}")
        return ""

    css_expr = config["css"]
    try:
        selector = CSSSelector(css_expr)
    except Exception as e:
        print(f"    ⚠️  Invalid CSS selector '{css_expr}': {e}")
        return ""

    elements = selector(tree)

    if not elements:
        print(f"    ⚠️  CSS selector matched nothing: {css_expr}")
        return ""

    element = elements[0]
    _remove_elements(element, config.get("remove", []))

    content_html = etree.tostring(element, encoding="unicode", method="html")
    base_url = config.get("base_url", "")
    if base_url:
        content_html = _fix_relative_urls(content_html, base_url)

    return content_html


def _remove_elements(root_element, removals: list):
    """Remove child elements matching the removal selectors."""
    for removal in removals:
        if "xpath" in removal:
            for el in root_element.xpath(removal["xpath"]):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
        elif "css" in removal:
            try:
                sel = CSSSelector(removal["css"])
                for el in sel(root_element):
                    parent = el.getparent()
                    if parent is not None:
                        parent.remove(el)
            except Exception:
                pass


def _convert_markdown(md_text: str, base_url: str) -> str:
    """Convert Markdown text to HTML."""
    # Strip YAML front matter
    md_text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", md_text, count=1, flags=re.DOTALL)

    html_content = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists", "toc"],
    )

    if base_url:
        html_content = _fix_relative_urls(html_content, base_url)

    return html_content


def _fix_relative_urls(html_str: str, base_url: str) -> str:
    """Make relative href and src attributes absolute."""
    base_url = base_url.rstrip("/")

    # Fix src="relative/path"
    html_str = re.sub(
        r'(src=["\'])(?!http|data:|//|#)(.*?)(["\'])',
        lambda m: f"{m.group(1)}{base_url}/{m.group(2)}{m.group(3)}",
        html_str,
    )
    # Fix href="relative/path"
    html_str = re.sub(
        r'(href=["\'])(?!http|mailto:|javascript:|//|#)(.*?)(["\'])',
        lambda m: f"{m.group(1)}{base_url}/{m.group(2)}{m.group(3)}",
        html_str,
    )
    return html_str


# ─── State Management ──────────────────────────────────────────────────────


def load_state() -> dict:
    """Load persisted state from JSON file."""
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Save state to JSON file."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ─── Source Checkers ───────────────────────────────────────────────────────


class SourceChecker:
    """Base class for all source checkers."""

    def __init__(self, source: dict, state: dict):
        self.source = source
        self.state = state
        self.source_id = source["id"]

    def check(self) -> list:
        raise NotImplementedError


class SequentialChecker(SourceChecker):
    """
    Checks sources that publish numbered issues (vol 1, 2, 3...).
    Probes the next number until it gets a 404.
    """

    def check(self) -> list:
        new_items = []
        state_key = f"{self.source_id}_latest"

        current = self.state.get(state_key, self.source["start"])

        url_template = self.source["url"]
        check_template = self.source.get("check_url", url_template)
        title_template = self.source.get("title", f"{self.source['name']} #{'{n}'}")
        summary_template = self.source.get("summary", "")
        tags = self.source.get("tags", [])
        content_config = self.source.get("content", None)

        # Check up to 5 ahead (in case we missed several)
        max_checks = 5
        checks_done = 0

        while checks_done < max_checks:
            next_n = current + 1
            check_url = check_template.format(n=next_n)
            print(f"  [{self.source['name']}] Checking #{next_n}...")

            if url_exists(check_url):
                item_url = url_template.format(n=next_n)
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                # Extract full content
                content_html = ""
                if content_config:
                    template_vars = {"n": next_n}
                    content_html = extract_content(
                        item_url, content_config, template_vars
                    )
                    if content_html:
                        size_kb = len(content_html.encode("utf-8")) / 1024
                        print(f"    📄 Content extracted: {size_kb:.1f} KB")
                    else:
                        print("    📄 No content extracted (will use summary only)")

                new_items.append(
                    {
                        "title": title_template.format(n=next_n),
                        "link": item_url,
                        "id": f"{self.source_id}-{next_n}",
                        "updated": now,
                        "summary": summary_template.format(n=next_n),
                        "content": content_html,
                        "source": self.source["name"],
                        "source_id": self.source_id,
                        "tags": tags,
                    }
                )
                print(f"    ✅ Found #{next_n}")
                current = next_n
            else:
                print(f"    ❌ #{next_n} not yet available")
                break

            checks_done += 1

        self.state[state_key] = current
        return new_items


class GitHubReleaseChecker(SourceChecker):
    """Watches a GitHub repository for new releases."""

    def check(self) -> list:
        new_items = []
        state_key = f"{self.source_id}_latest_release"
        repo = self.source["repo"]
        tags = self.source.get("tags", [])

        print(f"  [{self.source['name']}] Checking releases for {repo}...")

        api_url = f"https://api.github.com/repos/{repo}/releases?per_page=10"
        req = urllib.request.Request(api_url)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/vnd.github+json")

        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            releases = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"    ❌ API error: {e}")
            return []

        if not releases:
            print("    No releases found")
            return []

        last_known = self.state.get(state_key, "")

        # Find new releases (those we haven't seen)
        found_new = []
        for release in releases:
            if release["tag_name"] == last_known:
                break
            if not release.get("draft", False):
                found_new.append(release)

        # First run: just store current latest, don't emit items
        if not last_known:
            self.state[state_key] = releases[0]["tag_name"]
            print(f"    Initialized at {releases[0]['tag_name']}")
            return []

        # Process new releases (oldest first so feed order is correct)
        for release in reversed(found_new):
            tag = release["tag_name"]
            body = release.get("body", "") or ""
            name = release.get("name", tag) or tag
            published = release.get("published_at") or datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            title_template = self.source.get(
                "title", f"{self.source['name']} {'{tag}'}"
            )
            summary_template = self.source.get("summary", "{body}")

            # Convert release body to HTML
            content_html = ""
            if body:
                content_html = _convert_markdown(body, "")

            new_items.append(
                {
                    "title": title_template.format(tag=tag, name=name),
                    "link": release["html_url"],
                    "id": f"{self.source_id}-{tag}",
                    "updated": published,
                    "summary": summary_template.format(
                        body=body[:300], tag=tag, name=name
                    ),
                    "content": content_html,
                    "source": self.source["name"],
                    "source_id": self.source_id,
                    "tags": tags,
                }
            )
            print(f"    ✅ New release: {tag}")

        # Update state to latest
        if found_new:
            self.state[state_key] = found_new[0]["tag_name"]

        return new_items


class WebpageChecker(SourceChecker):
    """
    Watches a page for content changes.
    Compares a hash of the extracted content to detect updates.
    """

    def check(self) -> list:
        new_items = []
        state_key = f"{self.source_id}_hash"
        url = self.source["url"]
        tags = self.source.get("tags", [])
        content_config = self.source.get("content", None)

        print(f"  [{self.source['name']}] Checking {url}...")

        status, body = http_get(url)
        if status != 200 or not body:
            print(f"    ❌ Failed to fetch (HTTP {status})")
            return []

        # Extract content (used for both hashing and feed)
        content_html = ""
        if content_config:
            content_html = extract_content(url, content_config)
            hash_source = content_html
            if not content_html:
                # An empty extraction means the selector broke (page redesign,
                # anti-bot page, ...). Do NOT record it as a valid state,
                # otherwise the next run would treat it as "content changed".
                print("    ⚠️  Extracted content is empty — state left untouched")
                return []
        else:
            hash_source = body

        content_hash = hashlib.sha256(hash_source.strip().encode("utf-8")).hexdigest()[
            :20
        ]
        last_hash = self.state.get(state_key, "")

        if not last_hash:
            # First run: store hash, don't emit
            self.state[state_key] = content_hash
            print(f"    Initialized (hash: {content_hash})")
            return []

        if content_hash != last_hash:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            title_template = self.source.get("title", f"{self.source['name']} updated")
            summary_template = self.source.get("summary", "Page content has changed.")

            new_items.append(
                {
                    "title": title_template,
                    "link": url,
                    "id": f"{self.source_id}-{content_hash}",
                    "updated": now,
                    "summary": summary_template,
                    "content": content_html,
                    "source": self.source["name"],
                    "source_id": self.source_id,
                    "tags": tags,
                }
            )
            print(f"    ✅ Content changed! (old: {last_hash}, new: {content_hash})")
        else:
            print(f"    No changes (hash: {content_hash})")

        self.state[state_key] = content_hash
        return new_items


class WebpageItemsChecker(SourceChecker):
    """
    Extracts multiple items from a webpage using CSS selectors.
    Useful for journal TOC pages, blog listings, etc.
    """
    def check(self) -> list:
        new_items = []
        state_key = f"{self.source_id}_hash"
        url = self.source.get("url", "")
        tags = self.source.get("tags", [])
        items_config = self.source.get("items", {})
        
        print(f"  [{self.source['name']}] Checking {url}...")
        status, body = http_get(url)
        if status != 200 or not body:
            print(f"    ❌ Failed to fetch (HTTP {status})")
            return []
        
        # Parse HTML
        try:
            tree = lxml_html.fromstring(body)
        except Exception as e:
            print(f"    ⚠️  HTML parse error: {e}")
            return []
        
        # Get the base URL for resolving relative URLs
        base_url = url.rstrip("/")
        
        # Extract items using selector
        selector = items_config.get("selector", "")
        if not selector:
            print("    ⚠️  No selector configured")
            return []
        
        try:
            css_selector = CSSSelector(selector)
        except Exception as e:
            print(f"    ⚠️  Invalid CSS selector '{selector}': {e}")
            return []
        
        elements = css_selector(tree)
        if not elements:
            print(f"    ⚠️  CSS selector '{selector}' matched nothing")
            return []
        
        print(f"    Found {len(elements)} item(s)")

        now = _now()

        # Extract data from each item
        for elem in elements:
            item = {}
            
            # Extract title
            title_config = items_config.get("title", {})
            if title_config and "selector" in title_config:
                title_el = elem.cssselect(title_config["selector"])
                if title_el:
                    item["title"] = title_el[0].text_content().strip()
                    # Get link from title element if no separate link config
                    href = title_el[0].get("href")
                    if href:
                        item["link"] = self._resolve_url(href, base_url)
            
            # Extract link
            link_config = items_config.get("link", {})
            if link_config and "selector" in link_config:
                link_el = elem.cssselect(link_config["selector"])
                if link_el:
                    attr = link_config.get("attribute", "href")
                    item["link"] = self._resolve_url(link_el[0].get(attr, ""), base_url)
                elif not item.get("link"):
                    item["link"] = link_el[0].text_content().strip() if link_el else ""
            
            # Extract author. Set `multiple: true` to join every match
            # (e.g. all authors of a paper) instead of only the first one.
            author_config = items_config.get("author", {})
            if author_config and "selector" in author_config:
                author_el = elem.cssselect(author_config["selector"])
                if author_el:
                    names = []
                    picked = author_el if author_config.get("multiple") else author_el[:1]
                    for el in picked:
                        name = _clean_text(el.text_content()).rstrip(",")
                        if name and name not in names:
                            names.append(name)
                    if names:
                        item["author"] = author_config.get("separator", ", ").join(names)

            # Extract date. `attribute` reads any attribute (not just datetime)
            # and `regex` strips wrappers such as "Publication date: September, 2026".
            date_config = items_config.get("date", {})
            if date_config and "selector" in date_config:
                date_el = elem.cssselect(date_config["selector"])
                if date_el:
                    attr = date_config.get("attribute")
                    raw_date = date_el[0].get(attr) if attr else ""
                    if not raw_date:
                        raw_date = date_el[0].get("datetime") or ""
                    if not raw_date:
                        raw_date = date_el[0].text_content()
                    raw_date = _clean_text(raw_date)
                    pattern = date_config.get("regex")
                    if pattern and raw_date:
                        match = re.search(pattern, raw_date)
                        if match:
                            raw_date = (
                                match.group(1) if match.groups() else match.group(0)
                            ).strip()
                        else:
                            raw_date = ""
                    if raw_date:
                        item["date"] = raw_date
            
            # Extract description
            desc_config = items_config.get("description", {})
            if desc_config and "selector" in desc_config:
                desc_el = elem.cssselect(desc_config["selector"])
                if desc_el:
                    item["description"] = desc_el[0].text_content().strip()
            
            # Full article HTML. Deliberately NOT the whole <article> block:
            # a listing card is nearly all chrome — badges, aria labels and
            # images with relative URLs — and carries no abstract at all.
            # Readers that prefer <content:encoded> over <description>
            # (tt-rss among them) end up rendering that chrome and nothing
            # else. So leave the body empty unless the config points at a
            # real body selector.
            content_selector = items_config.get("content_selector")
            item["content"] = ""
            if content_selector:
                content_el = elem.cssselect(content_selector)
                if content_el:
                    item["content"] = etree.tostring(
                        content_el[0], encoding="unicode", method="html"
                    )
            
            # Normalize into the shape generate_feed() and main() expect.
            # Without source_id/updated the entry would be dropped from the
            # per-source feed and would emit an invalid empty <updated/>.
            title = item.get("title", "").strip()
            link = item.get("link", "") or url
            item_id = link or "{}-{}".format(
                self.source_id,
                hashlib.sha256(title.encode("utf-8")).hexdigest()[:12],
            )

            new_items.append(
                {
                    "title": title or "Untitled",
                    "link": link,
                    "id": item_id,
                    "updated": _parse_date(item.get("date", "")) or now,
                    "summary": item.get("description", ""),
                    "content": item.get("content", ""),
                    "author": item.get("author", ""),
                    "source": self.source["name"],
                    "source_id": self.source_id,
                    "tags": tags,
                }
            )
        
        # Hash the items, not the raw page: the page carries timestamps and
        # other noise that changes on every request. Sort the pairs first —
        # this listing reshuffles its order between requests, which would
        # otherwise look like a change every single run and re-trigger
        # enrichment for items we already have.
        items_hash = hashlib.sha256(
            str(sorted((i.get("title", ""), i.get("link", "")) for i in new_items)).encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        
        last_hash = self.state.get(state_key, "")
        # A brand-new source normally only seeds the hash, so adding one to
        # sources.yml does not dump the whole page into the feed at once.
        # For listings that only ever carry a handful of items (a journal TOC,
        # for example) that caution is unnecessary — `emit_on_init: true`
        # publishes the current items on the very first run.
        emit_on_init = bool(self.source.get("emit_on_init"))

        if not last_hash:
            self.state[state_key] = items_hash
            if emit_on_init:
                print(
                    f"    ✅ Initialized and emitting {len(new_items)} item(s) "
                    f"(hash: {items_hash})"
                )
                return new_items
            print(f"    Initialized (hash: {items_hash})")
            return []

        if items_hash != last_hash:
            print(f"    ✅ Content changed! (old: {last_hash}, new: {items_hash})")
            self.state[state_key] = items_hash
            return new_items

        print(f"    No changes (hash: {items_hash})")
        return []
    
    def _resolve_url(self, url: str, base_url: str) -> str:
        """Resolve relative URLs to absolute URLs."""
        if url.startswith("http") or url.startswith("//"):
            return url
        if url.startswith("/"):
            # Absolute path - need to get scheme+host from base_url
            from urllib.parse import urljoin
            return urljoin(base_url, url)
        return urljoin(base_url, url)


# ─── Checker Registry ──────────────────────────────────────────────────────

CHECKERS = {
    "sequential": SequentialChecker,
    "github_release": GitHubReleaseChecker,
    "webpage": WebpageChecker,
    "webpage_items": WebpageItemsChecker,
}


# ─── Feed I/O ─────────────────────────────────────────────────────────────

ATOM_NS = "http://www.w3.org/2005/Atom"


def _normalise_formats(value) -> list:
    """Validate the `feed.formats` setting from sources.yml."""
    if not value:
        return list(DEFAULT_FORMATS)
    if isinstance(value, str):
        value = [value]

    picked, unknown = [], []
    for raw in value:
        name = str(raw).strip().lower()
        if not name:
            continue
        (picked if name in KNOWN_FORMATS else unknown).append(name)

    if unknown:
        print(f"⚠️  Unknown feed format(s) {unknown}; use one of {list(KNOWN_FORMATS)}")
    if not picked:
        print(f"⚠️  No usable feed format; falling back to {list(DEFAULT_FORMATS)}")
        return list(DEFAULT_FORMATS)
    return picked


def _state_feed_path(formats: list) -> str:
    """Which previously written feed to restore items from.

    Atom carries the richer metadata, so it wins when it is being generated.
    The other file is the fallback — needed on the very first run after the
    format was switched, when the preferred file does not exist yet.
    """
    preferred = ATOM_FEED_FILE if "atom" in formats else FEED_FILE
    fallback = FEED_FILE if preferred == ATOM_FEED_FILE else ATOM_FEED_FILE
    return preferred if Path(preferred).exists() else fallback


def _blank_item() -> dict:
    return {
        "title": "", "link": "", "id": "", "updated": "", "summary": "",
        "content": "", "author": "", "source": "", "source_id": "", "tags": [],
    }


def _items_from_atom(root) -> list:
    """Read entries out of an Atom 1.0 feed."""
    ns = {"atom": ATOM_NS}
    items = []
    for entry in root.findall("atom:entry", ns):
        item = _blank_item()

        for field, path in (
            ("title", "atom:title"),
            ("id", "atom:id"),
            ("updated", "atom:updated"),
            ("summary", "atom:summary"),
            ("content", "atom:content"),
        ):
            el = entry.find(path, ns)
            if el is not None and el.text:
                item[field] = el.text

        el = entry.find("atom:link", ns)
        if el is not None:
            item["link"] = el.get("href", "")

        # Read the author back too. Every feed is rebuilt from scratch on
        # each run, so skipping this field silently dropped the author of
        # every older entry once it was reloaded from feed.xml.
        el = entry.find("atom:author/atom:name", ns)
        if el is not None and el.text:
            item["author"] = el.text

        for cat in entry.findall("atom:category", ns):
            term = cat.get("term", "")
            scheme = cat.get("scheme", "")
            if scheme == "source":
                item["source"] = term
            elif scheme == "source_id":
                item["source_id"] = term
            elif term:
                item["tags"].append(term)

        items.append(item)
    return items


def _items_from_rss(root) -> list:
    """Read items out of the RSS 2.0 feed this script writes.

    `<description>` is stored as HTML, so it has to be flattened back to the
    plain-text `summary` the generator expects — otherwise every rebuild would
    wrap the previous markup in yet another <p> and accumulate it.
    """
    ns = {"dc": DC_NS, "content": CONTENT_NS}
    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for entry in channel.findall("item"):
        item = _blank_item()

        def text(tag):
            el = entry.find(tag)
            return el.text or "" if el is not None else ""

        item["title"] = text("title").strip()
        item["link"] = text("link").strip()
        item["id"] = text("guid").strip() or item["link"]

        pub = text("pubDate").strip()
        item["updated"] = _iso_from_rfc822(pub) or _parse_date(pub)

        item["summary"] = _strip_markup(text("description"))

        el = entry.find("content:encoded", ns)
        if el is not None and el.text:
            item["content"] = el.text

        el = entry.find("dc:creator", ns)
        if el is not None and el.text:
            item["author"] = el.text.strip()
        elif entry.findtext("author"):
            item["author"] = entry.findtext("author").strip()

        for cat in entry.findall("category"):
            term = (cat.text or "").strip()
            domain = (cat.get("domain") or cat.get("scheme") or "").strip()
            if not term:
                continue
            if domain == "source":
                item["source"] = term
            elif domain == "source_id":
                item["source_id"] = term
            else:
                item["tags"].append(term)

        items.append(item)
    return items


def load_existing_items(path: str = None) -> list:
    """Parse a previously generated feed and return its items.

    Both flavours are accepted: the file is rebuilt from scratch on every run,
    so whatever we wrote last time has to be readable back regardless of
    whether it was RSS 2.0 or Atom.
    """
    candidates = [path] if path else [ATOM_FEED_FILE, FEED_FILE]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            root = parse(candidate).getroot()
        except Exception as e:
            print(f"⚠️  Could not parse existing feed {candidate}: {e}")
            continue

        tag = root.tag
        if tag == "rss":
            items = _items_from_rss(root)
        elif tag == f"{{{ATOM_NS}}}feed" or tag == "feed":
            items = _items_from_atom(root)
        else:
            print(f"⚠️  Unrecognised feed root <{tag}> in {candidate}")
            continue

        print(f"📂 Restored {len(items)} item(s) from {candidate} "
              f"({'RSS 2.0' if tag == 'rss' else 'Atom'})")
        return items

    return []


def generate_feed(items: list, feed_config: dict, file_path: str):
    """Generate an Atom XML feed file."""
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)

    base_url = feed_config.get("base_url", "https://example.com").rstrip("/")
    filename = Path(file_path).name
    feed_url = f"{base_url}/{filename}"

    # Build XML
    feed = Element("feed")
    feed.set("xmlns", ATOM_NS)

    el = SubElement(feed, "title")
    el.text = feed_config.get("title", "Feed Aggregator")

    el = SubElement(feed, "subtitle")
    el.text = feed_config.get("subtitle", "")

    SubElement(feed, "link", href=feed_url, rel="self", type="application/atom+xml")
    SubElement(feed, "link", href=base_url, rel="alternate", type="text/html")

    el = SubElement(feed, "id")
    el.text = feed_url

    el = SubElement(feed, "updated")
    el.text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    el = SubElement(feed, "generator")
    el.text = "rss-aggregator"

    author = SubElement(feed, "author")
    el = SubElement(author, "name")
    el.text = feed_config.get("author", "Bot")

    # Entries
    for item in items[:MAX_FEED_ITEMS]:
        entry = SubElement(feed, "entry")

        el = SubElement(entry, "title")
        el.text = item.get("title", "Untitled")

        SubElement(
            entry, "link", href=item.get("link", ""), rel="alternate", type="text/html"
        )

        el = SubElement(entry, "id")
        el.text = item.get("id", item.get("link", ""))

        el = SubElement(entry, "updated")
        el.text = item.get("updated") or _now()

        if item.get("author"):
            entry_author = SubElement(entry, "author")
            el = SubElement(entry_author, "name")
            el.text = item["author"]

        # Summary (plain text, shown in list views)
        if item.get("summary"):
            el = SubElement(entry, "summary", type="text")
            el.text = item["summary"]

        # Full content (HTML, shown when you open the item). Skipped when the
        # stored HTML is listing chrome rather than a body — see
        # _usable_content: an empty <content> sends the reader back to
        # <summary>, which is the abstract.
        body = _usable_content(item.get("content", ""), item.get("summary", ""))
        if body:
            el = SubElement(entry, "content", type="html")
            el.text = body

        # Source tags
        if item.get("source"):
            SubElement(
                entry,
                "category",
                term=item["source"],
                scheme="source",
                label=item["source"],
            )
        if item.get("source_id"):
            SubElement(
                entry,
                "category",
                term=item["source_id"],
                scheme="source_id",
                label=item["source_id"],
            )

        # Tags
        for tag in item.get("tags", []):
            SubElement(entry, "category", term=tag, label=tag)

    # Serialize to file
    xml_str = tostring(feed, encoding="unicode", xml_declaration=False)
    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

    if not xml_str.endswith("\n"):
        xml_str += "\n"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(xml_str)

    size_kb = Path(file_path).stat().st_size / 1024
    print(
        f"📄 Written {file_path} ({len(items[:MAX_FEED_ITEMS])} items, {size_kb:.1f} KB)"
    )


# ─── RSS 2.0 output ────────────────────────────────────────────────────────
#
# The Atom feed was perfectly valid, yet a surprising number of readers show
# nothing but the headline for it: they only look at RSS 2.0's <description>.
# So every feed is now written as RSS 2.0 as well, with the abstract wrapped
# in <p> inside a CDATA section — the shape big publishers (Springer, Elsevier)
# ship and the shape readers reliably render.


def _xml_text(raw) -> str:
    """Escape a value for use as XML character data."""
    return html_escape("" if raw is None else str(raw), quote=True)


def _cdata(raw) -> str:
    """Wrap a chunk of HTML so it survives as literal markup."""
    text = "" if raw is None else str(raw)
    # A literal ]]> would terminate the section early.
    text = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


def _paragraphs(raw: str) -> str:
    """Turn plain text into one or more <p> elements."""
    text = (raw or "").strip()
    if not text:
        return ""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "".join(f"<p>{_xml_text(_clean_text(b))}</p>" for b in blocks)


def _usable_content(content: str, summary: str) -> str:
    """Return the stored body only when it really is one.

    Older runs put the whole listing card in here. That HTML is chrome — no
    abstract, badges, images with relative URLs — and a reader that prefers
    <content:encoded> over <description> (tt-rss, for one) then shows that
    chrome and hides the abstract entirely. A real body contains the
    abstract, so use that as the test; anything else is dropped and the
    reader falls back to <description>.
    """
    if not content:
        return ""
    text = _strip_markup(content)
    if not text:
        return ""
    probe = (summary or "").strip()[:100]
    if probe and probe in text:
        return content
    # No abstract to compare against? Keep it only if there is enough prose
    # for it to plausibly be an article body.
    return content if len(text) > 1000 else ""


def _description_html(item: dict) -> str:
    """Build the <description> body: the abstract, or a fallback if missing."""
    html = _paragraphs(item.get("summary", ""))
    if html:
        return html

    # No abstract resolved (no DOI, API miss...). Fall back to the scraped
    # snippet so the reader still shows something under the headline.
    fallback = _strip_markup(
        _usable_content(item.get("content", ""), item.get("summary", ""))
    )
    if fallback:
        if len(fallback) > 500:
            fallback = fallback[:500].rstrip() + "…"
        return f"<p>{_xml_text(fallback)}</p>"
    return ""


def generate_rss_feed(items: list, feed_config: dict, file_path: str):
    """Generate an RSS 2.0 feed file."""
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)

    base_url = feed_config.get("base_url", "https://example.com").rstrip("/")
    filename = Path(file_path).name
    feed_url = f"{base_url}/{filename}"
    now = datetime.now(timezone.utc)

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"'
        f' xmlns:dc="{DC_NS}"'
        f' xmlns:content="{CONTENT_NS}"'
        ' xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{_xml_text(feed_config.get('title', 'Feed Aggregator'))}</title>",
        f"    <link>{_xml_text(base_url)}</link>",
        f"    <description>{_xml_text(feed_config.get('subtitle', ''))}</description>",
        f"    <language>{_xml_text(feed_config.get('language', 'en-us'))}</language>",
        f"    <lastBuildDate>{_rfc822(now.strftime('%Y-%m-%dT%H:%M:%SZ'))}</lastBuildDate>",
        "    <generator>rss-aggregator</generator>",
        f"    <ttl>{int(feed_config.get('ttl', 60))}</ttl>",
        "    <docs>https://www.rssboard.org/rss-specification</docs>",
        f'    <atom:link href="{_xml_text(feed_url)}" rel="self"'
        ' type="application/rss+xml" />',
    ]

    for item in items[:MAX_FEED_ITEMS]:
        out.append("    <item>")
        out.append(f"      <title>{_xml_text(item.get('title', 'Untitled'))}</title>")

        link = item.get("link", "")
        out.append(f"      <link>{_xml_text(link)}</link>")

        description = _description_html(item)
        out.append(f"      <description>{_cdata(description)}</description>")

        pub = _rfc822(item.get("updated") or _now())
        if pub:
            out.append(f"      <pubDate>{pub}</pubDate>")

        guid = item.get("id") or link
        if guid:
            perma = "true" if str(guid).startswith("http") else "false"
            out.append(
                f'      <guid isPermaLink="{perma}">{_xml_text(guid)}</guid>'
            )

        if item.get("author"):
            out.append(
                f"      <dc:creator>{_xml_text(item['author'])}</dc:creator>"
            )

        # Only emit <content:encoded> when there is a genuine body. Both
        # description and content:encoded present means some readers take the
        # *last* one — so a junk body would win over the abstract.
        body = _usable_content(item.get("content", ""), item.get("summary", ""))
        if body:
            out.append(
                f"      <content:encoded>{_cdata(body)}</content:encoded>"
            )

        if item.get("source"):
            out.append(
                '      <category domain="source">'
                f"{_xml_text(item['source'])}</category>"
            )
        if item.get("source_id"):
            out.append(
                '      <category domain="source_id">'
                f"{_xml_text(item['source_id'])}</category>"
            )
        for tag in item.get("tags", []):
            out.append(f"      <category>{_xml_text(tag)}</category>")

        out.append("    </item>")

    out.append("  </channel>")
    out.append("</rss>")

    xml_str = "\n".join(out) + "\n"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(xml_str)

    size_kb = Path(file_path).stat().st_size / 1024
    print(
        f"📄 Written {file_path} (RSS 2.0, {len(items[:MAX_FEED_ITEMS])} items,"
        f" {size_kb:.1f} KB)"
    )


# ─── Index Page Generation ─────────────────────────────────────────────────


def generate_index_html(sources: list, feed_config: dict, formats: list = None):
    """Generate index.html from template and sources.yml data."""
    base_url = feed_config.get("base_url", "https://example.com").rstrip("/")
    repo_url = feed_config.get("repo_url", "#")
    feed_url = f"{base_url}/feed.xml"
    formats = formats or list(DEFAULT_FORMATS)

    # Load template
    template_path = Path(INDEX_TEMPLATE)
    if not template_path.exists():
        print(f"⚠️  Template not found at {INDEX_TEMPLATE}, skipping index generation.")
        return

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    # Split feed title for hero display
    title = feed_config.get("title", "RSS Aggregator")
    title_words = title.split()
    if len(title_words) >= 2:
        hero_title = " ".join(title_words[:-1])
        hero_subtitle = title_words[-1]
    else:
        hero_title = title
        hero_subtitle = "FEEDS"

    # Build source cards HTML
    cards_html = ""
    for source in sources:
        s_id = source["id"]
        s_name = html_escape(source.get("name", s_id))
        s_feed_url = f"{base_url}/{s_id}.xml"
        s_atom_url = f"{base_url}/{s_id}.atom.xml"
        s_tags = source.get("tags", [])

        # Determine a visit URL (resolve template with start number)
        s_url = source.get("url", "")
        if "{n}" in s_url:
            s_url = s_url.format(n=source.get("start", 1))

        # Build tags HTML
        tags_html = ""
        for tag in s_tags:
            tags_html += f'          <span class="card-tag">{html_escape(tag)}</span>\n'

        # Both flavours get a copy button; RSS 2.0 is the one most readers
        # render, Atom is there for clients that prefer it.
        actions_html = (
            f'          <button class="card-btn" '
            f"onclick=\"copyFeed(this, '{html_escape(s_feed_url)}')\">⚡ RSS</button>\n"
        )
        if "atom" in formats:
            actions_html += (
                f'          <button class="card-btn secondary" '
                f"onclick=\"copyFeed(this, '{html_escape(s_atom_url)}')\">⚛ ATOM</button>\n"
            )
        actions_html += (
            f'          <a class="card-btn secondary" '
            f'href="{html_escape(s_url)}" target="_blank" rel="noopener">↗ SITE</a>\n'
        )

        cards_html += f'''      <div class="card">
        <h3 class="card-title">{s_name}</h3>
        <div class="card-tags">
{tags_html}        </div>
        <div class="card-actions">
{actions_html}        </div>
      </div>
'''

    # Replace all placeholders
    html_content = template
    html_content = html_content.replace("<!-- FEED_TITLE -->", html_escape(title))
    html_content = html_content.replace(
        "<!-- FEED_SUBTITLE -->", html_escape(feed_config.get("subtitle", ""))
    )
    html_content = html_content.replace("<!-- HERO_TITLE -->", html_escape(hero_title))
    html_content = html_content.replace(
        "<!-- HERO_SUBTITLE -->", html_escape(hero_subtitle)
    )
    html_content = html_content.replace("<!-- BASE_URL -->", html_escape(base_url))
    html_content = html_content.replace("<!-- FEED_URL -->", html_escape(feed_url))
    html_content = html_content.replace("<!-- REPO_URL -->", html_escape(repo_url))
    html_content = html_content.replace("<!-- TOTAL_SOURCES -->", str(len(sources)))
    html_content = html_content.replace("<!-- SOURCE_CARDS -->", cards_html)

    # <link rel="alternate"> tags: the canonical RSS 2.0 feed plus Atom when
    # it is generated, so feed auto-discovery finds both.
    alternates = []
    if "rss" in formats:
        alternates.append(
            '  <link rel="alternate" type="application/rss+xml" '
            f'title="{html_escape(title)} (RSS 2.0)" href="{html_escape(feed_url)}">'
        )
    if "atom" in formats:
        alternates.append(
            '  <link rel="alternate" type="application/atom+xml" '
            f'title="{html_escape(title)} (Atom)"'
            f' href="{html_escape(base_url + "/feed.atom.xml")}">'
        )
    html_content = html_content.replace(
        "<!-- FEED_ALTERNATES -->", "\n".join(alternates)
    )

    # Write output
    Path("docs").mkdir(parents=True, exist_ok=True)
    if not html_content.endswith("\n"):
        html_content += "\n"
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

    print("📄 Written docs/index.html")


# ─── Main ─────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  RSS Aggregator - Checking for updates")
    print("=" * 60)
    print()

    # Load config
    if not Path(SOURCES_FILE).exists():
        print(f"❌ {SOURCES_FILE} not found!")
        return 1

    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # A broken sources.yml used to crash the whole run with a bare
        # traceback. Report it clearly so the failing line is obvious.
        print(f"❌ {SOURCES_FILE} is not valid YAML:")
        print(f"   {e}")
        return 1

    if not isinstance(config, dict):
        print(f"❌ {SOURCES_FILE} must contain a YAML mapping at the top level.")
        return 1

    feed_config = config.get("feed", {})
    sources = config.get("sources", []) or []

    if not sources:
        print("⚠️  No sources configured.")
        return 0

    formats = _normalise_formats(feed_config.get("formats"))
    print(f"📄 Feed format(s): {', '.join(formats)}")

    # Load state and existing feed
    state = load_state()
    existing_items = load_existing_items(_state_feed_path(formats))
    print(
        f"📂 Loaded state ({len(state)} keys) and {len(existing_items)} existing items\n"
    )

    # Check each source
    all_new_items = []
    errors = []

    print(f"🔍 Checking {len(sources)} source(s):\n")
    for source in sources:
        source_id = source.get("id", "unknown")
        source_type = source.get("type", "")

        checker_cls = CHECKERS.get(source_type)
        if not checker_cls:
            msg = f"Unknown source type '{source_type}' for '{source_id}'"
            print(f"  ⚠️  {msg}")
            errors.append(msg)
            continue

        checker = checker_cls(source, state)
        try:
            new_items = checker.check()
            # Only hits the network for genuinely new items, so an unchanged
            # source costs nothing.
            filled = enrich_items(
                new_items, source.get("enrich"), source.get("name", source_id)
            )
            if filled:
                print(f"  ✨ Enriched {filled} item(s) with abstracts")
            all_new_items.extend(new_items)
        except Exception as e:
            msg = f"Error checking '{source_id}': {e}"
            print(f"  ⚠️  {msg}")
            traceback.print_exc()
            errors.append(msg)

        print()  # Blank line between sources

    # Summary
    print("─" * 60)
    has_updates = len(all_new_items) > 0

    if has_updates:
        print(f"🎉 Found {len(all_new_items)} new item(s)!")
        merged = all_new_items + existing_items
    else:
        print("😴 No new items found.")
        merged = existing_items

    # Newest first, then drop repeats (webpage_items re-emits the whole list
    # on every change, which used to pile up duplicates).
    all_items = dedupe_items(merged)
    dropped = len(merged) - len(all_items)
    if dropped:
        print(f"🧹 Removed {dropped} duplicated item(s)")

    if errors:
        print(f"⚠️  {len(errors)} error(s) occurred.")

    # Generate main feed, in each configured flavour
    if "rss" in formats:
        generate_rss_feed(all_items, feed_config, FEED_FILE)
    if "atom" in formats:
        generate_feed(all_items, feed_config, ATOM_FEED_FILE)

    # Generate individual feeds (use source name as title, no prefix)
    for source in sources:
        s_id = source["id"]
        s_items = [item for item in all_items if item.get("source_id") == s_id]
        fc = feed_config.copy()
        fc["title"] = source.get("name", s_id)
        if "rss" in formats:
            generate_rss_feed(s_items, fc, f"docs/{s_id}.xml")
        if "atom" in formats:
            generate_feed(s_items, fc, f"docs/{s_id}.atom.xml")

    # Generate HTML index
    generate_index_html(sources, feed_config, formats)

    save_state(state)

    # GitHub Actions output
    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_updates={'true' if has_updates else 'false'}\n")
            f.write(f"new_count={len(all_new_items)}\n")
            f.write(f"total_count={len(all_items[:MAX_FEED_ITEMS])}\n")

    print("\n✅ Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())

