import json
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


SOURCE_URL = "https://sage.cnpereading.com/toc/LTR"
JOURNAL_URL = "https://sage.cnpereading.com/"
JOURNAL_TITLE = "Language Teaching Research"

FEED_TITLE = "Language Teaching Research - OnlineFirst"
FEED_DESCRIPTION = (
    "Latest OnlineFirst articles from Language Teaching Research."
)

RSS_FILE = Path("docs/feeds/ltr-onlinefirst.xml")
DATA_FILE = Path("data/ltr_articles.json")

# RSS 中保留多少篇
MAX_RSS_ITEMS = 50

# 历史数据库最多保留多少篇
MAX_DATABASE_ITEMS = 1000


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sage.cnpereading.com/",
}


# ============================================================
# 基础工具
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = BeautifulSoup(
        str(text),
        "html.parser"
    ).get_text(
        " ",
        strip=True
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def normalize_url(url):
    if not url:
        return ""

    return urljoin(
        SOURCE_URL,
        url.strip()
    )


def extract_doi(text):
    if not text:
        return ""

    match = re.search(
        r"10\.\d{4,9}/[-._;()/:A-Z0-9]+",
        text,
        re.I
    )

    if not match:
        return ""

    doi = match.group(0)

    return doi.rstrip(
        ".,;)"
    )


def parse_date(text):
    """
    SAGE 当前页面主要显示：

        First published 2026

    也兼容：

        First published September, 2026
        First published September 7, 2026
        2026-09-07
    """

    if not text:
        return ""

    text = clean_text(text)

    # YYYY-MM-DD
    m = re.search(
        r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b",
        text
    )

    if m:
        dt = datetime(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            tzinfo=timezone.utc
        )

        return dt.isoformat()

    # Month, YYYY
    months = (
        "January|February|March|April|May|June|July|"
        "August|September|October|November|December"
    )

    m = re.search(
        rf"\b({months}),?\s+(20\d{{2}})\b",
        text,
        re.I
    )

    if m:
        try:
            dt = datetime.strptime(
                f"{m.group(1)} {m.group(2)}",
                "%B %Y"
            )

            return dt.replace(
                tzinfo=timezone.utc
            ).isoformat()

        except ValueError:
            pass

    # YYYY
    m = re.search(
        r"\b(20\d{2})\b",
        text
    )

    if m:
        dt = datetime(
            int(m.group(1)),
            1,
            1,
            tzinfo=timezone.utc
        )

        return dt.isoformat()

    return ""


# ============================================================
# 下载 OnlineFirst 首页
# ============================================================

def fetch_page():

    print(
        f"Fetching {SOURCE_URL}"
    )

    response = requests.get(
        SOURCE_URL,
        headers=HEADERS,
        timeout=60
    )

    print(
        "HTTP:",
        response.status_code
    )

    response.raise_for_status()

    html = response.text

    if len(html) < 5000:
        raise RuntimeError(
            "Downloaded page is too small."
        )

    if "Language Teaching Research" not in html:
        raise RuntimeError(
            "Expected journal name not found."
        )

    if "OnlineFirst" not in html:
        raise RuntimeError(
            "OnlineFirst marker not found."
        )

    return html


# ============================================================
# 找文章容器
# ============================================================

def get_container(link):

    current = link

    for _ in range(8):

        if current is None:
            break

        text = clean_text(
            current.get_text(
                " ",
                strip=True
            )
        )

        # 当前 SAGE 每篇文章的标题 + 作者 +
        # 摘要通常都在这个范围内。
        if len(text) >= 300:
            return current

        current = current.parent

    return link.parent


# ============================================================
# 提取作者
# ============================================================

def extract_authors(container, title):

    candidates = []

    # 优先使用明显的 author class
    for node in container.select(
        "[class*='author'], "
        "[class*='Author'], "
        ".authors"
    ):

        text = clean_text(
            node.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        if text == title:
            continue

        if len(text) > 500:
            continue

        candidates.append(text)

    if candidates:

        # 去重
        result = []

        for x in candidates:

            if x not in result:
                result.append(x)

        return "; ".join(result[:5])

    # 当前页面的作者一般紧跟标题之后。
    # 找到标题所在 h3，再检查后面的几个元素。

    heading = None

    for h in container.find_all(
        ["h2", "h3", "h4"]
    ):

        if clean_text(
            h.get_text(
                " ",
                strip=True
            )
        ) == title:

            heading = h
            break

    if heading:

        for node in heading.find_all_next(
            limit=8
        ):

            text = clean_text(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            if not text:
                continue

            if text == title:
                continue

            if "Preview abstract" in text:
                continue

            if "Get Access" in text:
                continue

            if text.startswith(
                "Abstract"
            ):
                break

            if len(text) < 200:
                return text

    return ""


# ============================================================
# 提取摘要
# ============================================================

def extract_abstract(container):

    # 当前页面直接显示 Abstract
    # 找到 Abstract 后面较长的文本。

    abstract_text = []

    found = False

    for node in container.find_all(
        ["p", "div"]
    ):

        text = clean_text(
            node.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        if text.lower() == "abstract":
            found = True
            continue

        if found:

            if "Get Access" in text:
                continue

            if "Preview abstract" in text:
                continue

            if len(text) >= 50:

                abstract_text.append(
                    text
                )

    if abstract_text:

        return " ".join(
            abstract_text
        )[:8000]

    return ""


# ============================================================
# 解析文章
# ============================================================

def parse_articles(html):

    soup = BeautifulSoup(
        html,
        "lxml"
    )

    articles = []

    # 当前 SAGE 页面文章链接使用 /doi/
    for link in soup.find_all(
        "a",
        href=True
    ):

        href = link.get(
            "href",
            ""
        )

        if "/doi/" not in href.lower():
            continue

        title = clean_text(
            link.get_text(
                " ",
                strip=True
            )
        )

        if len(title) < 10:
            continue

        # 避免把导航、重复链接等当成文章
        if title in {
            "OnlineFirst",
            "Abstract",
            "Get Access",
            "PDF",
            "Download",
        }:
            continue

        url = normalize_url(
            href
        )

        container = get_container(
            link
        )

        container_text = clean_text(
            container.get_text(
                " ",
                strip=True
            )
        )

        authors = extract_authors(
            container,
            title
        )

        abstract = extract_abstract(
            container
        )

        published = parse_date(
            container_text
        )

        doi = extract_doi(
            url + " " + container_text
        )

        # ----------------------------------------------------
        # 类型
        # ----------------------------------------------------

        article_type = "Research article"

        if "Review article" in container_text:
            article_type = "Review article"

        elif "Editorial" in container_text:
            article_type = "Editorial"

        elif "Correction" in container_text:
            article_type = "Correction"

        elif "Retraction" in container_text:
            article_type = "Retraction"

        # ----------------------------------------------------
        # Access
        # ----------------------------------------------------

        if "Open access" in container_text:
            access = "Open access"
        else:
            access = "Restricted access"

        article = {
            "title": title,
            "url": url,
            "authors": authors,
            "abstract": abstract,
            "published": published,
            "doi": doi,
            "type": article_type,
            "access": access,
        }

        articles.append(
            article
        )

    # --------------------------------------------------------
    # URL 去重
    # --------------------------------------------------------

    unique = {}

    for article in articles:

        key = (
            article["url"]
            or article["doi"]
            or article["title"]
        ).lower()

        if key not in unique:
            unique[key] = article

    articles = list(
        unique.values()
    )

    print(
        f"Detected {len(articles)} "
        f"article(s) on current page."
    )

    return articles


# ============================================================
# 历史数据库
# ============================================================

def load_database():

    if not DATA_FILE.exists():
        return []

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception as exc:

        print(
            "Database load failed:",
            exc
        )

    return []


def save_database(articles):

    DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        DATA_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            articles,
            f,
            ensure_ascii=False,
            indent=2
        )


def merge_articles(
    old_articles,
    current_articles
):

    merged = {}

    # 旧数据先放进去
    for article in old_articles:

        key = (
            article.get("doi")
            or article.get("url")
            or article.get("title")
        ).lower()

        merged[key] = article

    # 当前页面覆盖旧数据
    for article in current_articles:

        key = (
            article.get("doi")
            or article.get("url")
            or article.get("title")
        ).lower()

        if key in merged:

            old = merged[key]

            # 只补充/更新当前抓到的信息
            for field, value in article.items():

                if value:
                    old[field] = value

        else:

            merged[key] = article

    result = list(
        merged.values()
    )

    result.sort(
        key=lambda x: x.get(
            "published",
            ""
        ),
        reverse=True
    )

    return result[
        :MAX_DATABASE_ITEMS
    ]


# ============================================================
# RSS
# ============================================================

def generate_rss(articles):

    RSS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom":
                "http://www.w3.org/2005/Atom",
            "xmlns:dc":
                "http://purl.org/dc/elements/1.1/",
        }
    )

    channel = ET.SubElement(
        rss,
        "channel"
    )

    ET.SubElement(
        channel,
        "title"
    ).text = FEED_TITLE

    ET.SubElement(
        channel,
        "link"
    ).text = SOURCE_URL

    ET.SubElement(
        channel,
        "description"
    ).text = FEED_DESCRIPTION

    ET.SubElement(
        channel,
        "language"
    ).text = "en"

    ET.SubElement(
        channel,
        "ttl"
    ).text = "1440"

    atom_link = ET.SubElement(
        channel,
        "{http://www.w3.org/2005/Atom}link"
    )

    atom_link.set(
        "rel",
        "self"
    )

    # 这里暂时使用相对可修改的占位。
    # 发布前可以在 GitHub Actions 中替换。
    atom_link.set(
        "href",
        "https://YOUR-USERNAME.github.io/YOUR-REPO/"
        "feeds/ltr-onlinefirst.xml"
    )

    atom_link.set(
        "type",
        "application/rss+xml"
    )

    # --------------------------------------------------------
    # RSS items
    # --------------------------------------------------------

    for article in articles[
        :MAX_RSS_ITEMS
    ]:

        item = ET.SubElement(
            channel,
            "item"
        )

        title = article.get(
            "title",
            ""
        )

        url = article.get(
            "url",
            ""
        )

        authors = article.get(
            "authors",
            ""
        )

        abstract = article.get(
            "abstract",
            ""
        )

        published = article.get(
            "published",
            ""
        )

        doi = article.get(
            "doi",
            ""
        )

        article_type = article.get(
            "type",
            ""
        )

        access = article.get(
            "access",
            ""
        )

        ET.SubElement(
            item,
            "title"
        ).text = title

        ET.SubElement(
            item,
            "link"
        ).text = url

        ET.SubElement(
            item,
            "guid",
            {
                "isPermaLink": "true"
            }
        ).text = url

        # 日期
        try:

            dt = datetime.fromisoformat(
                published
            )

            pub_date = format_datetime(
                dt
            )

        except Exception:

            pub_date = format_datetime(
                datetime.now(
                    timezone.utc
                )
            )

        ET.SubElement(
            item,
            "pubDate"
        ).text = pub_date

        # 作者
        if authors:

            ET.SubElement(
                item,
                "{http://purl.org/dc/elements/1.1/}"
                "creator"
            ).text = authors

        # DOI
        if doi:

            ET.SubElement(
                item,
                "{http://purl.org/dc/elements/1.1/}"
                "identifier"
            ).text = f"doi:{doi}"

        # 类型
        if article_type:

            ET.SubElement(
                item,
                "category"
            ).text = article_type

        # Open Access
        if access:

            ET.SubElement(
                item,
                "category"
            ).text = access

        # ----------------------------------------------------
        # Description
        # ----------------------------------------------------

        parts = []

        if authors:

            parts.append(
                "<p><strong>Authors:</strong> "
                + escape(authors)
                + "</p>"
            )

        if doi:

            parts.append(
                "<p><strong>DOI:</strong> "
                + escape(doi)
                + "</p>"
            )

        if access:

            parts.append(
                "<p><strong>Access:</strong> "
                + escape(access)
                + "</p>"
            )

        if abstract:

            parts.append(
                "<p><strong>Abstract:</strong></p>"
                "<p>"
                + escape(abstract)
                + "</p>"
            )

        ET.SubElement(
            item,
            "description"
        ).text = "".join(
            parts
        )

    # --------------------------------------------------------
    # XML 输出
    # --------------------------------------------------------

    tree = ET.ElementTree(
        rss
    )

    ET.indent(
        tree,
        space="  "
    )

    tree.write(
        RSS_FILE,
        encoding="utf-8",
        xml_declaration=True
    )

    print(
        f"RSS generated: {RSS_FILE}"
    )

    print(
        "RSS items:",
        min(
            len(articles),
            MAX_RSS_ITEMS
        )
    )


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 60)
    print(
        "Language Teaching Research "
        "OnlineFirst RSS"
    )
    print("=" * 60)

    old_articles = load_database()

    print(
        "Historical database:",
        len(old_articles)
    )

    try:

        html = fetch_page()

        current_articles = parse_articles(
            html
        )

        # ----------------------------------------------------
        # 安全检查
        # ----------------------------------------------------

        if len(current_articles) < 5:

            raise RuntimeError(
                "Less than 5 articles detected. "
                "The page structure may have changed."
            )

        print(
            "Current page:",
            len(current_articles)
        )

        # ----------------------------------------------------
        # 合并
        # ----------------------------------------------------

        all_articles = merge_articles(
            old_articles,
            current_articles
        )

        print(
            "Total database:",
            len(all_articles)
        )

        # ----------------------------------------------------
        # 保存
        # ----------------------------------------------------

        save_database(
            all_articles
        )

        # ----------------------------------------------------
        # RSS
        # ----------------------------------------------------

        generate_rss(
            all_articles
        )

        print(
            "SUCCESS"
        )

    except Exception as exc:

        print(
            "ERROR:",
            repr(exc)
        )

        # 不覆盖已有 RSS
        if RSS_FILE.exists():

            print(
                "Keeping previous RSS."
            )

            sys.exit(0)

        sys.exit(1)


if __name__ == "__main__":
    main()
