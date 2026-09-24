# Site to RSS Monitor

A powerful, lightweight, and extensible tool powered by GitHub Actions that monitors website changes and converts them into high-quality RSS (Atom) feeds. Perfect for sites that don't provide their own feeds.

## Key Features

- **Multi-Source Aggregation**: Subscribe to a single feed that combines all your monitored sources.
- **Individual Feeds**: Every source gets its own dedicated Atom feed, allowing you to subscribe only to what you care about.
- **Full Content Extraction**: Supports HTML (XPath/CSS), Markdown, and GitHub Release bodies.
- **Dynamic Index Page**: Automatically generates a clean landing page listing all available sources and their feed links.
- **Zero Maintenance**: Runs entirely on GitHub Actions; no server or database required.
- **Change Detection**: Smart hashing to detect when a webpage has actually changed.

## Quick Start

### 1. Subscribe
The main aggregated feed is available at:
`https://yc2398.github.io/site-to-rss/feed.xml`

Or visit the [Landing Page](https://yc2398.github.io/site-to-rss/) to find individual feed links.

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
      regex: "Published:\s*(.+)" # strip wrappers around the date
    description:
      selector: ".abstract"
  tags: [journal]
```

## How it Works

1. **Schedule**: A GitHub Action runs every 6 hours (configurable in `.github/workflows/check-updates.yml`).
2. **Fetch & Extract**: The Python script (`scripts/check_updates.py`) reads `sources.yml`, fetches the target pages, and extracts content using `lxml` and `cssselect`.
3. **State Management**: It tracks the last seen issue or content hash in `state.json`.
4. **Deploy**: New entries are committed to the repo, and GitHub Pages is updated with the latest XML feeds and `index.html`.

## Contributing

Have a source you want to share?
1. Open a PR adding the source to `sources.yml`.
2. Once merged, it will be automatically included in the aggregated feed and get its own dedicated feed.
