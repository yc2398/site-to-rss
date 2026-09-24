"""
Extensible RSS feed generator with full content extraction.
Reads sources.yml, checks for new content, generates an Atom feed.
"""

import hashlib
import json
import os
import re
import sys
import traceback
import urllib.request
import urllib.error
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
INDEX_TEMPLATE = "templates/index_template.html"
MAX_FEED_ITEMS = 100
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


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


# ─── Checker Registry ──────────────────────────────────────────────────────

CHECKERS = {
    "sequential": SequentialChecker,
    "github_release": GitHubReleaseChecker,
    "webpage": WebpageChecker,
}


# ─── Feed I/O ─────────────────────────────────────────────────────────────

ATOM_NS = "http://www.w3.org/2005/Atom"


def load_existing_items() -> list:
    """Parse existing feed.xml and return items."""
    items = []
    if not Path(FEED_FILE).exists():
        return items

    try:
        tree = parse(FEED_FILE)
        root = tree.getroot()
        ns = {"atom": ATOM_NS}

        for entry in root.findall("atom:entry", ns):
            item = {
                "title": "",
                "link": "",
                "id": "",
                "updated": "",
                "summary": "",
                "content": "",
                "source": "",
                "source_id": "",
                "tags": [],
            }

            el = entry.find("atom:title", ns)
            if el is not None and el.text:
                item["title"] = el.text

            el = entry.find("atom:link", ns)
            if el is not None:
                item["link"] = el.get("href", "")

            el = entry.find("atom:id", ns)
            if el is not None and el.text:
                item["id"] = el.text

            el = entry.find("atom:updated", ns)
            if el is not None and el.text:
                item["updated"] = el.text

            el = entry.find("atom:summary", ns)
            if el is not None and el.text:
                item["summary"] = el.text

            el = entry.find("atom:content", ns)
            if el is not None and el.text:
                item["content"] = el.text

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
    except Exception as e:
        print(f"⚠️  Could not parse existing feed: {e}")

    return items


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
        el.text = item.get("updated", "")

        # Summary (plain text, shown in list views)
        if item.get("summary"):
            el = SubElement(entry, "summary", type="text")
            el.text = item["summary"]

        # Full content (HTML, shown when you open the item)
        if item.get("content"):
            el = SubElement(entry, "content", type="html")
            el.text = item["content"]

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


# ─── Index Page Generation ─────────────────────────────────────────────────


def generate_index_html(sources: list, feed_config: dict):
    """Generate index.html from template and sources.yml data."""
    base_url = feed_config.get("base_url", "https://example.com").rstrip("/")
    repo_url = feed_config.get("repo_url", "#")
    feed_url = f"{base_url}/feed.xml"

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
        s_tags = source.get("tags", [])

        # Determine a visit URL (resolve template with start number)
        s_url = source.get("url", "")
        if "{n}" in s_url:
            s_url = s_url.format(n=source.get("start", 1))

        # Build tags HTML
        tags_html = ""
        for tag in s_tags:
            tags_html += f'          <span class="card-tag">{html_escape(tag)}</span>\n'

        cards_html += f'''      <div class="card">
        <h3 class="card-title">{s_name}</h3>
        <div class="card-tags">
{tags_html}        </div>
        <div class="card-actions">
          <button class="card-btn" onclick="copyFeed(this, '{html_escape(s_feed_url)}')">⚡ SUBSCRIBE</button>
          <a class="card-btn secondary" href="{html_escape(s_url)}" target="_blank" rel="noopener">↗ VISIT</a>
        </div>
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

    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    feed_config = config.get("feed", {})
    sources = config.get("sources", [])

    if not sources:
        print("⚠️  No sources configured.")
        return 0

    # Load state and existing feed
    state = load_state()
    existing_items = load_existing_items()
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
        all_items = all_new_items + existing_items
    else:
        print("😴 No new items found.")
        all_items = existing_items

    if errors:
        print(f"⚠️  {len(errors)} error(s) occurred.")

    # Generate main feed
    generate_feed(all_items, feed_config, FEED_FILE)

    # Generate individual feeds (use source name as title, no prefix)
    for source in sources:
        s_id = source["id"]
        s_items = [item for item in all_items if item.get("source_id") == s_id]
        fc = feed_config.copy()
        fc["title"] = source.get("name", s_id)
        generate_feed(s_items, fc, f"docs/{s_id}.xml")

    # Generate HTML index
    generate_index_html(sources, feed_config)

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
