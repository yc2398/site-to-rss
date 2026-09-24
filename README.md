# Site to RSS Monitor

A powerful, lightweight, and extensible tool powered by GitHub Actions that monitors website changes and converts them into high-quality RSS 2.0 / Atom feeds. Perfect for sites that don't provide their own feeds.

## Key Features

- **Multi-Source Aggregation**: Subscribe to a single feed that combines all your monitored sources.
- **Dual Format Output**: Every feed is published as **RSS 2.0** *and* Atom, so any reader shows the abstract — not just the headline.
- **Individual Feeds**: Every source gets its own dedicated feed, in both formats.
- **Full Content Extraction**: Supports HTML (XPath/CSS), Markdown, and GitHub Release bodies.
- **Dynamic Index Page**: Automatically generates a clean landing page listing all available sources and their feed links.
- **Zero Maintenance**: Runs entirely on GitHub Actions; no server or database required.
- **Change Detection**: Smart hashing to detect when a webpage has actually changed.
- **Metadata Enrichment**: Pull missing abstracts from Crossref / OpenAlex when a source's detail page is unreachable.

## Quick Start

### 1. Subscribe
The main aggregated feed is available at:
`https://yc2398.github.io/site-to-rss/feed.xml` (RSS 2.0)

The same content as Atom:
`https://yc2398.github.io/site-to-rss/feed.atom.xml`

Or visit the [Landing Page](https://yc2398.github.io/site-to-rss/) to find individual feed links.

> **Which one should I use?** `.xml` (RSS 2.0) if your reader shows no abstract —
> a fair number of readers only render RSS 2.0's `<description>`. `.atom.xml` if
> you prefer Atom. Both carry identical content.

### 2. Host Your Own
1. **Fork this repository**.
2. **Configure**: Edit `sources.yml`. Update `base_url` and `repo_url` to your own GitHub Pages URL and repository.
3. **Enable Pages**: Go to **Settings** → **Pages** → Build and deployment → Source: **GitHub Actions**.
4. **Manual Run**: Go to **Actions** → **Check for Updates** → **Run workflow** to initialize your feeds.

## Configuration (`sources.yml`)

Adding a new source is as simple as adding a few lines of YAML. No Python knowledge required.

### Supported Source Types

#### 1. Sequential (Numbered Issues)
Best for newsletters or periodicals with predictable URLs (e.g., `vol-121`, `issue-42`).
```yaml
- id: my-newsletter
  name: My Newsletter
  type: sequential
  start: 100
  url: "https://example.com/issue/{n}"
  content:
    css: ".article-body"
    type: html
```

#### 2. Webpage (Change Detection)
Monitors a specific page and triggers an update whenever the content changes.
```yaml
- id: dev-blog
  name: Dev Blog
  type: webpage
  url: "https://example.com/blog"
  content:
    xpath: "//main"
    type: html
```

#### 3. GitHub Release
Converts GitHub repository releases into RSS entries.
```yaml
- id: my-tool
  name: My Tool Releases
  type: github_release
  repo: "owner/repo"
  content:
    type: release_body
```

#### 4. Webpage Items (Listing Pages)
Scrapes every item on a listing page (journal TOCs, blog indexes, ...). Each item becomes its own entry.
```yaml
- id: my-journal
  name: My Journal
  type: webpage_items
  url: "https://example.com/journal"
  items:
    selector: "article"          # one CSS selector per item
    title:
      selector: "h3 a"
    link:
      selector: "h3 a"
      attribute: "href"
    author:
      selector: ".author"
      multiple: true             # join every match instead of only the first
    date:
      selector: ".pub-date"
      attribute: "aria-label"    # read any attribute, not just datetime
      regex: 'Published:\s*(.+)' # strip wrappers around the date
    description:
      selector: ".abstract"
  tags: [journal]
```

> ⚠️ **Always quote regexes with single quotes.** In YAML double quotes `\s`
> is an illegal escape and the whole file fails to parse.

#### First run behaviour (`emit_on_init`)

By default a new `webpage_items` source only records a hash on its first run
and emits nothing — otherwise adding a source would dump the entire page into
your feed at once. The items appear from the second run onwards, as the page
changes.

A journal TOC only ever lists a handful of papers, so waiting is pointless.
Set `emit_on_init: true` to publish the current items immediately:

```yaml
- id: my-journal
  name: My Journal
  type: webpage_items
  url: "https://example.com/journal"
  emit_on_init: true      # publish on the first run instead of waiting
  items:
    selector: "article"
    title: {selector: "h3 a"}
    link: {selector: "h3 a", attribute: "href"}
  tags: [journal]
```

### Enrichment (`enrich`)

Listing pages often carry only a title, a link and an author — the abstract
lives on the detail page, which some publishers put behind a bot challenge
(`sage.cnpereading.com`, for example, serves a WAF slider captcha on its
`/doi/...` pages). Instead of fighting the WAF, `enrich` resolves each item
through its DOI in a scholarly metadata API and writes the abstract into the
entry (RSS 2.0 `<description>`, Atom `<summary>`):

```yaml
- id: my-journal
  name: My Journal
  type: webpage_items
  url: "https://example.com/journal"
  items:
    selector: "article"
    title: {selector: "h3 a"}
    link: {selector: "h3 a", attribute: "href"}
  enrich:
    provider: crossref        # crossref | openalex
    fallback: openalex        # tried if the primary provider has no abstract
    key: link                 # item field to read the DOI from
    doi_regex: '10\.\d{4,9}/[^/?#]+'
    max_items: 20             # optional cap on API calls per run
    max_chars: 500            # optional summary truncation
  tags: [journal]
```

Notes:

- Items that already have a `description`/`summary` are left untouched, so a
  listing page that does carry an abstract always wins.
- Enrichment only runs for **new** items — an unchanged source costs zero API
  calls.
- Both providers are public and need no API key. Be reasonable with
  `max_items`; Crossref asks heavy users to join its [polite pool](https://github.com/CrossRef/rest-api-doc#good-manners).

### Feed formats (`feed.formats`)

Every source is published in two flavours:

| Setting | File | Where the abstract lands |
| --- | --- | --- |
| `rss` | `<name>.xml` | `<description>` — a CDATA-wrapped `<p>` block |
| `atom` | `<name>.atom.xml` | `<summary>` |

RSS 2.0 items also carry `<pubDate>` (RFC 822), `<guid>`, the author in
`<dc:creator>`, tags in `<category>` and the scraped HTML in
`<content:encoded>`.

```yaml
feed:
  formats: [rss, atom]   # default — use [rss] or [atom] to publish just one
  language: "en-us"      # RSS 2.0 <language>
  ttl: 60                # RSS 2.0 <ttl>, in minutes
```

Existing entries are restored from whichever file was written last, so
switching formats (or dropping one) never loses items or abstracts.

### Reusing config with YAML anchors

Several sources often share the same platform and therefore the same
selectors. Define the block once and reference it, instead of copy-pasting it
into every source:

```yaml
.sage_items: &sage_items      # the leading dot keeps it out of the feed
  selector: "article"
  title: {selector: "h3 a"}
  link: {selector: "h3 a", attribute: "href"}

sources:
  - id: journal-a
    name: Journal A
    type: webpage_items
    url: "https://example.com/a"
    items: *sage_items
```

A source that needs one different field can still merge and override:

```yaml
    items:
      <<: *sage_items            # start from the shared block
      title: {selector: "h2 a"}  # replace just this field
```

## How it Works

1. **Schedule**: A GitHub Action runs every 6 hours (configurable in `.github/workflows/check-updates.yml`).
2. **Fetch & Extract**: The Python script (`scripts/check_updates.py`) reads `sources.yml`, fetches the target pages, and extracts content using `lxml` and `cssselect`.
3. **State Management**: It tracks the last seen issue or content hash in `state.json`.
4. **Deploy**: New entries are committed to the repo, and GitHub Pages is updated with the latest XML feeds and `index.html`.
5. **Format Coverage**: Each feed is written twice — RSS 2.0 (`.xml`) and Atom (`.atom.xml`) — because readers differ in which one they actually render.

## Contributing

Have a source you want to share?
1. Open a PR adding the source to `sources.yml`.
2. Once merged, it will be automatically included in the aggregated feed and get its own dedicated feed.
