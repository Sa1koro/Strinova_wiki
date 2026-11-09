#!/usr/bin/env python3
"""
Generic MediaWiki crawler:
1) Enumerates pages from MediaWiki API
2) Parses page HTML
3) Cleans noisy wiki-specific nodes
4) Converts HTML to Markdown
5) Rewrites internal links to local Markdown files (redirect-aware)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Pattern, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md


REMOVE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "audio",
    "video",
    ".mw-editsection",
    ".mw-cite-backlink",
    # MediaWiki marks direct links to media files (mp3/png...) with "internal".
    "a.internal",
    ".catlinks",
    ".printfooter",
    ".mw-authority-control",
    ".metadata",
    "table.navbox",
    "div.navbox",
    "table.vertical-navbox",
    "div.hatnote.navigation-not-searchable",
    # Table of contents and breadcrumbs only duplicate headings/links.
    "#toc",
    ".toc",
    "[class*=breadcrumb]",
)

# Query keys that mark UI links (edit, history, purge, short url...), not articles.
ACTION_QUERY_KEYS = {"action", "oldid", "diff", "curid", "printable", "veaction", "wpDestFile"}

# Characters that are illegal in file names or break Markdown/Obsidian links.
UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]()\s]+')
LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\(")

# Namespaces whose links point to media, not articles: File(6), Media(-2).
MEDIA_NAMESPACE_IDS = {"6", "-2"}


DEFAULT_EXCLUDE_TITLE_PATTERNS = [
    r"Category:.*",
    r"Template:.*",
    r"User:.*",
    r"Special:.*",
    r"Help:.*",
    r"MediaWiki:.*",
    r"Talk:.*",
    r"File:.*",
    r"Image:.*",
    # Common localized namespace prefixes on Chinese wikis.
    r"分类:.*",
    r"模板:.*",
    r"用户:.*",
    r"特殊:.*",
    r"帮助:.*",
    r"媒体:.*",
    r"讨论:.*",
    r"文件:.*",
]


def normalize_title(title: str) -> str:
    title = unquote(title or "")
    title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def title_key(title: str) -> str:
    """Lookup key for titles; MediaWiki treats the first letter case-insensitively."""
    title = normalize_title(title)
    return title[:1].upper() + title[1:]


def safe_stem_from_title(title: str, max_len: int = 60) -> str:
    # Keep CJK and other Unicode; only replace characters unsafe for paths/links.
    # 60 chars keeps CJK names (3 bytes each) under the 255-byte filename limit.
    stem = UNSAFE_FILENAME_RE.sub("_", title.strip())
    stem = re.sub(r"_+", "_", stem).strip("._")
    if not stem:
        stem = "page"
    return stem[:max_len].rstrip("._") or "page"


def compile_title_patterns(patterns: List[str]) -> List[Pattern[str]]:
    compiled: List[Pattern[str]] = []
    for pattern in patterns:
        compiled.append(re.compile(pattern, re.IGNORECASE))
    return compiled


@dataclass(frozen=True)
class PageInfo:
    pageid: int
    ns: int
    title: str
    is_redirect: bool


class MediaWikiCrawler:
    def __init__(
        self,
        wiki_url: str,
        api_url: str,
        output_dir: Path,
        timeout: int = 25,
        max_retries: int = 4,
        user_agent: str = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        min_request_interval: float = 0.25,
        keep_images: bool = False,
        keep_external_links: bool = False,
        extra_remove_selectors: Optional[List[str]] = None,
    ) -> None:
        self.wiki_url = wiki_url.rstrip("/") + "/"
        self.api_url = api_url
        self.output_dir = output_dir
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.keep_images = keep_images
        self.keep_external_links = keep_external_links
        self.remove_selectors = list(REMOVE_SELECTORS) + list(extra_remove_selectors or [])
        self.session = requests.Session()
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0

        parsed = urlparse(self.wiki_url)
        self.root_scheme = parsed.scheme
        self.root_host = parsed.netloc

        self.siteinfo = {}
        self.article_prefix = "/"
        self.script_path = "/index.php"
        self.category_prefix = "Category"
        self.media_prefixes: Set[str] = {"file", "image", "media"}
        # Redirect title key -> target title key, filled by fetch_redirect_map().
        self.redirects: Dict[str, str] = {}

    def api_get(self, params: Dict[str, object]) -> Dict[str, object]:
        payload = dict(params)
        payload.setdefault("format", "json")
        payload.setdefault("formatversion", 2)
        payload.setdefault("utf8", 1)

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json,text/javascript,*/*;q=0.1",
            "Referer": self.wiki_url,
        }

        for attempt in range(self.max_retries):
            try:
                if self.min_request_interval > 0:
                    with self._request_lock:
                        now = time.monotonic()
                        wait = self.min_request_interval - (now - self._last_request_at)
                        if wait > 0:
                            time.sleep(wait)
                        self._last_request_at = time.monotonic()

                resp = self.session.get(
                    self.api_url,
                    params=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(f"MediaWiki error: {data['error']}")
                return data
            except Exception as exc:  # pylint: disable=broad-except
                if attempt >= self.max_retries - 1:
                    raise
                backoff = 1.5 * (2**attempt)
                print(
                    f"[warn] request failed ({exc}); retry in {backoff:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(backoff)

        raise RuntimeError("unreachable")

    def fetch_siteinfo(self) -> Dict[str, object]:
        data = self.api_get(
            {
                "action": "query",
                "meta": "siteinfo",
                "siprop": "general|namespaces|namespacealiases",
            }
        )
        query = data.get("query", {})
        general = query.get("general", {}) or {}
        namespaces = query.get("namespaces", {}) or {}
        aliases = query.get("namespacealiases", []) or []

        for ns_id, value in namespaces.items():
            if str(ns_id) in MEDIA_NAMESPACE_IDS and isinstance(value, dict):
                for key in ("name", "canonical"):
                    if value.get(key):
                        self.media_prefixes.add(str(value[key]).lower())
        for alias in aliases:
            if str(alias.get("id")) in MEDIA_NAMESPACE_IDS and alias.get("alias"):
                self.media_prefixes.add(str(alias["alias"]).lower())

        self.siteinfo = {
            "general": general,
            "namespaces": namespaces,
        }

        article_path = str(general.get("articlepath", "/$1"))
        script = str(general.get("script", "/index.php"))

        # articlepath looks like /wiki/$1, keep prefix before $1
        prefix = article_path.split("$1")[0]
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        self.article_prefix = prefix
        self.script_path = script if script.startswith("/") else f"/{script}"
        ns_14 = namespaces.get("14", {}) if isinstance(namespaces, dict) else {}
        if isinstance(ns_14, dict):
            name = str(ns_14.get("name", "")).strip()
            canonical = str(ns_14.get("canonical", "")).strip()
            self.category_prefix = name or canonical or "Category"
        return self.siteinfo

    def normalize_category_title(self, raw_category: str) -> str:
        title = normalize_title(raw_category)
        if ":" in title:
            return title
        return f"{self.category_prefix}:{title}"

    def content_namespace_ids(self) -> List[int]:
        if not self.siteinfo:
            self.fetch_siteinfo()
        namespaces = self.siteinfo["namespaces"]

        ids = []
        for key, value in namespaces.items():
            ns_id = int(key)
            if ns_id < 0:
                continue
            if isinstance(value, dict) and "content" in value:
                ids.append(ns_id)

        if not ids:
            return [0]
        return sorted(set(ids))

    def resolve_namespace_ids(self, namespaces_arg: str, content_only: bool) -> List[int]:
        if not self.siteinfo:
            self.fetch_siteinfo()
        namespaces = self.siteinfo["namespaces"]

        all_ns = sorted(int(key) for key in namespaces.keys())
        if namespaces_arg.lower() == "all":
            namespace_ids = [ns for ns in all_ns if ns >= 0]
            if content_only:
                content_ns = set(self.content_namespace_ids())
                namespace_ids = [ns for ns in namespace_ids if ns in content_ns]
            return namespace_ids

        values = []
        for item in namespaces_arg.split(","):
            item = item.strip()
            if not item:
                continue
            values.append(int(item))
        return sorted(set(values))

    def list_pages(
        self,
        namespace_ids: Iterable[int],
        include_redirects: bool,
        max_pages: Optional[int],
        exclude_title_patterns: List[Pattern[str]],
    ) -> List[PageInfo]:
        pages: List[PageInfo] = []

        for ns in namespace_ids:
            continuation: Dict[str, object] = {}
            while True:
                params: Dict[str, object] = {
                    "action": "query",
                    "list": "allpages",
                    "apnamespace": ns,
                    "aplimit": "max",
                }
                params.update(continuation)
                data = self.api_get(params)

                allpages = data.get("query", {}).get("allpages", []) or []
                for item in allpages:
                    page = PageInfo(
                        pageid=int(item["pageid"]),
                        ns=int(item["ns"]),
                        title=str(item["title"]),
                        is_redirect=bool(item.get("redirect", False)),
                    )
                    if not include_redirects and page.is_redirect:
                        continue
                    if any(pattern.match(page.title) for pattern in exclude_title_patterns):
                        continue
                    pages.append(page)
                    if max_pages and len(pages) >= max_pages:
                        return pages

                if "continue" not in data:
                    break
                continuation = data["continue"]

        return pages

    def list_pages_from_categories(
        self,
        category_titles: List[str],
        recursive_subcategories: bool,
        max_pages: Optional[int],
        exclude_title_patterns: List[Pattern[str]],
    ) -> List[PageInfo]:
        if not self.siteinfo:
            self.fetch_siteinfo()

        page_by_id: Dict[int, PageInfo] = {}
        page_by_title: Dict[str, PageInfo] = {}

        queue = deque(self.normalize_category_title(item) for item in category_titles if item)
        visited_categories = set()

        while queue:
            category = queue.popleft()
            category_key = normalize_title(category).lower()
            if category_key in visited_categories:
                continue
            visited_categories.add(category_key)

            continuation: Dict[str, object] = {}
            while True:
                params: Dict[str, object] = {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": category,
                    "cmlimit": "max",
                    # page: content/help/etc pages, subcat: child categories.
                    # We intentionally exclude "file" here.
                    "cmtype": "page|subcat",
                }
                params.update(continuation)
                data = self.api_get(params)

                members = data.get("query", {}).get("categorymembers", []) or []
                for item in members:
                    ns = int(item.get("ns", -999))
                    title = str(item.get("title", ""))
                    if not title:
                        continue

                    if ns == 14:
                        if recursive_subcategories:
                            queue.append(title)
                        continue

                    if any(pattern.match(title) for pattern in exclude_title_patterns):
                        continue

                    page = PageInfo(
                        pageid=int(item.get("pageid", 0)),
                        ns=ns,
                        title=title,
                        # categorymembers does not expose redirect flag reliably.
                        is_redirect=False,
                    )
                    if page.pageid > 0:
                        page_by_id[page.pageid] = page
                    else:
                        page_by_title[normalize_title(page.title)] = page

                    if max_pages and (len(page_by_id) + len(page_by_title)) >= max_pages:
                        combined = list(page_by_id.values()) + list(page_by_title.values())
                        return combined

                if "continue" not in data:
                    break
                continuation = data["continue"]

        return list(page_by_id.values()) + list(page_by_title.values())

    def list_pages_by_titles(self, titles: List[str]) -> List[PageInfo]:
        """Resolve explicit page titles (following redirects) to PageInfo."""
        pages: Dict[int, PageInfo] = {}
        for start in range(0, len(titles), 50):
            batch = [normalize_title(t) for t in titles[start : start + 50] if t]
            data = self.api_get(
                {"action": "query", "titles": "|".join(batch), "redirects": 1}
            )
            for item in data.get("query", {}).get("pages", []) or []:
                if item.get("missing") or item.get("invalid"):
                    print(f"[warn] page not found: {item.get('title')}", file=sys.stderr)
                    continue
                page = PageInfo(
                    pageid=int(item["pageid"]),
                    ns=int(item["ns"]),
                    title=str(item["title"]),
                    is_redirect=False,
                )
                pages[page.pageid] = page
        return list(pages.values())

    def fetch_redirect_map(self, pages: List[PageInfo]) -> Dict[str, str]:
        """Map every redirect pointing at a crawled page to that page's title key."""
        page_ids = [p.pageid for p in pages if p.pageid > 0]
        redirects: Dict[str, str] = {}
        for start in range(0, len(page_ids), 50):
            batch = page_ids[start : start + 50]
            continuation: Dict[str, object] = {}
            while True:
                params: Dict[str, object] = {
                    "action": "query",
                    "prop": "redirects",
                    "pageids": "|".join(str(i) for i in batch),
                    "rdprop": "title",
                    "rdlimit": "max",
                }
                params.update(continuation)
                data = self.api_get(params)
                for item in data.get("query", {}).get("pages", []) or []:
                    target = title_key(str(item.get("title", "")))
                    for rd in item.get("redirects", []) or []:
                        redirects[title_key(str(rd["title"]))] = target
                if "continue" not in data:
                    break
                continuation = data["continue"]
        self.redirects = redirects
        return redirects

    def parse_page(self, title: str) -> Dict[str, object]:
        return self.api_get(
            {
                "action": "parse",
                "page": title,
                "redirects": 1,
                "prop": "text|categories",
            }
        ).get("parse", {})

    def extract_title_from_href(self, href: str) -> Optional[str]:
        if not href or href.startswith("#"):
            return None
        if href.startswith("mailto:") or href.startswith("javascript:"):
            return None

        full = urljoin(self.wiki_url, href)
        parsed = urlparse(full)
        if parsed.netloc != self.root_host:
            return None

        query = parse_qs(parsed.query)
        if "title" in query and query["title"]:
            return normalize_title(query["title"][0])

        path = parsed.path
        if path.startswith(self.article_prefix):
            title = path[len(self.article_prefix) :]
            if title and not path.startswith(self.script_path):
                return normalize_title(title)

        return None

    def is_action_href(self, href: str) -> bool:
        """True for same-site UI links such as edit/history/purge/short-url."""
        parsed = urlparse(urljoin(self.wiki_url, href))
        if parsed.netloc != self.root_host:
            return False
        return bool(ACTION_QUERY_KEYS & set(parse_qs(parsed.query)))

    def is_media_title(self, title: str) -> bool:
        prefix, sep, _ = title.partition(":")
        return bool(sep) and prefix.strip().lower() in self.media_prefixes

    def resolve_local_file(self, title: str, title_to_file: Dict[str, str]) -> Optional[str]:
        key = title_key(title.split("#", 1)[0])
        key = self.redirects.get(key, key)
        return title_to_file.get(key)

    def clean_and_rewrite_html(
        self,
        html: str,
        title_to_file: Dict[str, str],
    ) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for selector in self.remove_selectors:
            for node in soup.select(selector):
                node.decompose()

        # Remove references and citation backlinks for cleaner persona KB text.
        for node in soup.select("sup.reference, span.reference, ol.references, div.reflist"):
            node.decompose()

        for img in soup.find_all("img"):
            src = img.get("src")
            if not self.keep_images:
                img.decompose()
            elif src:
                img["src"] = urljoin(self.wiki_url, src)

        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            # Image-only anchors are empty once images are dropped.
            if not anchor.get_text(strip=True) and not anchor.find("img"):
                anchor.decompose()
                continue
            # Self links and in-page anchors carry no information in a KB.
            if not href or href.startswith("#"):
                anchor.unwrap()
                continue
            if self.is_action_href(href):
                anchor.decompose()
                continue

            target_title = self.extract_title_from_href(href)
            if target_title:
                local_file = self.resolve_local_file(target_title, title_to_file)
                if local_file:
                    anchor["href"] = f"./{local_file}"
                    anchor.attrs.pop("title", None)
                    continue
                if self.is_media_title(target_title):
                    # Drop raw file names (e.g. "媒体文件:x.mp3"), keep real captions.
                    file_name = target_title.partition(":")[2].strip()
                    if file_name and file_name in anchor.get_text():
                        anchor.decompose()
                    else:
                        anchor.unwrap()
                    continue

            if self.keep_external_links:
                anchor["href"] = urljoin(self.wiki_url, href)
            else:
                anchor.unwrap()

        return str(soup)

    @staticmethod
    def html_to_markdown(html: str) -> str:
        text = md(
            html,
            heading_style="ATX",
            bullets="-",
            escape_asterisks=False,
            escape_underscores=False,
            strip=["span"],
        )
        text = text.replace("\r\n", "\n")
        # Link text spanning several lines (block elements inside <a>) breaks links.
        text = LINK_TEXT_RE.sub(lambda m: "[" + " ".join(m.group(1).split()) + "](", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        # Headings directly after lists/paragraphs need a blank line to be parsed.
        text = re.sub(r"\n(#{1,6} )", r"\n\n\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"

    def page_url(self, title: str) -> str:
        path = self.article_prefix + quote(title.replace(" ", "_"), safe="/:")
        return urljoin(self.wiki_url, path)


def build_filename_mapping(pages: List[PageInfo]) -> Dict[str, str]:
    """Map title key -> readable file name; stable across runs (ordered by pageid)."""
    used = set()
    mapping: Dict[str, str] = {}

    for page in sorted(pages, key=lambda p: p.pageid):
        stem = safe_stem_from_title(page.title)
        filename = f"{stem}.md"
        # Lower-cased check: macOS/Windows file systems are case-insensitive.
        if filename.lower() in used:
            filename = f"{stem}_{page.pageid}.md"

        used.add(filename.lower())
        mapping[title_key(page.title)] = filename

    return mapping


def write_page_markdown(
    crawler: MediaWikiCrawler,
    page: PageInfo,
    title_to_file: Dict[str, str],
    pages_dir: Path,
) -> Tuple[bool, str, List[str]]:
    try:
        parsed = crawler.parse_page(page.title)
        html = parsed.get("text", "")
        if not html:
            return (False, f"empty content: {page.title}", [])

        cleaned_html = crawler.clean_and_rewrite_html(html, title_to_file)
        markdown_body = crawler.html_to_markdown(cleaned_html)

        # formatversion=2 returns {"category": ...}; v1 used "*".
        categories = []
        for item in parsed.get("categories", []) or []:
            name = item.get("category") or item.get("*")
            if name and not item.get("hidden"):
                categories.append(normalize_title(str(name)))

        out_file = pages_dir / title_to_file[title_key(page.title)]

        # json.dumps output is valid YAML and handles quotes/colons safely.
        yaml_lines = [
            "---",
            f"title: {json.dumps(page.title, ensure_ascii=False)}",
            f"pageid: {page.pageid}",
            f"namespace: {page.ns}",
            f"source: {crawler.page_url(page.title)}",
            f"categories: {json.dumps(categories, ensure_ascii=False)}",
            "---",
            "",
            f"# {page.title}",
            "",
            "",
        ]

        content = "\n".join(yaml_lines) + markdown_body
        out_file.write_text(content, encoding="utf-8")
        return (True, page.title, categories)
    except Exception as exc:  # pylint: disable=broad-except
        return (False, f"{page.title}: {exc}", [])


def write_indexes(
    output_dir: Path,
    pages: List[PageInfo],
    title_to_file: Dict[str, str],
    siteinfo: Dict[str, object],
    namespace_ids: List[int],
    page_categories: Dict[int, List[str]],
) -> None:
    pages_sorted = sorted(pages, key=lambda p: normalize_title(p.title).lower())
    records = []
    for p in pages_sorted:
        records.append(
            {
                "title": p.title,
                "pageid": p.pageid,
                "namespace": p.ns,
                "redirect": p.is_redirect,
                "categories": page_categories.get(p.pageid, []),
                "file": f"pages/{title_to_file[title_key(p.title)]}",
            }
        )

    index_json = {
        "site": siteinfo.get("general", {}),
        "namespaces": namespace_ids,
        "page_count": len(records),
        "pages": records,
    }
    (output_dir / "index.json").write_text(
        json.dumps(index_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    readme_lines = [
        "# MediaWiki Markdown Export",
        "",
        f"- Total pages: **{len(records)}**",
        f"- Namespaces: `{','.join(str(ns) for ns in namespace_ids)}`",
        "- Markdown files are in `pages/`",
        "- Cross-page links are rewritten to local markdown links",
        "- Complete index metadata is in `index.json`",
        "",
        "## Example pages",
        "",
    ]

    for item in records[:30]:
        readme_lines.append(f"- [{item['title']}]({item['file']})")
    if len(records) > 30:
        readme_lines.append(f"- ... and {len(records) - 30} more pages")

    (output_dir / "README.md").write_text(
        "\n".join(readme_lines) + "\n", encoding="utf-8"
    )


def build_api_url(wiki_url: str, api_url: Optional[str]) -> str:
    if api_url:
        return api_url
    return urljoin(wiki_url.rstrip("/") + "/", "api.php")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl MediaWiki pages and convert to linked Markdown files."
    )
    parser.add_argument(
        "--wiki-url",
        required=True,
        help="Wiki root URL, e.g. https://wiki.biligame.com/klbq/",
    )
    parser.add_argument(
        "--api-url",
        default="",
        help="Optional MediaWiki API URL. Defaults to <wiki-url>/api.php",
    )
    parser.add_argument(
        "--output-dir",
        default="klbq",
        help="Output directory for markdown export.",
    )
    parser.add_argument(
        "--namespaces",
        default="all",
        help='Comma-separated namespace ids, or "all". Default: all',
    )
    parser.add_argument(
        "--include-category",
        action="append",
        default=[],
        help=(
            "Only crawl pages under this category title. "
            'Can be repeated. Example: "分类:内容页面"'
        ),
    )
    parser.add_argument(
        "--page",
        action="append",
        default=[],
        help=(
            "Only crawl this exact page title. Can be repeated; takes precedence "
            'over --include-category. Example: "星绘"'
        ),
    )
    category_group = parser.add_mutually_exclusive_group()
    category_group.add_argument(
        "--category-recursive",
        dest="category_recursive",
        action="store_true",
        default=True,
        help="When using --include-category, recurse into subcategories (default).",
    )
    category_group.add_argument(
        "--no-category-recursive",
        dest="category_recursive",
        action="store_false",
        help="When using --include-category, do not recurse into subcategories.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional cap for number of pages (0 means unlimited).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent workers for page parsing (default: 1).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="HTTP timeout seconds (default: 25).",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.25,
        help="Minimum delay between API requests in seconds (default: 0.25).",
    )
    parser.add_argument(
        "--include-redirects",
        action="store_true",
        help="Include redirect pages in output.",
    )
    parser.add_argument(
        "--exclude-title-regex",
        action="append",
        default=[],
        help="Regex pattern for page titles to exclude. Can be repeated.",
    )
    parser.add_argument(
        "--no-default-exclude-titles",
        action="store_true",
        help="Disable built-in noisy-title exclusions.",
    )
    parser.add_argument(
        "--remove-selector",
        action="append",
        default=[],
        help="Extra CSS selector to strip before conversion. Can be repeated.",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep images as Markdown image links (dropped by default).",
    )
    parser.add_argument(
        "--keep-external-links",
        action="store_true",
        help=(
            "Keep URLs for external links and wiki pages outside the crawl set "
            "(by default only the link text is kept)."
        ),
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Delete existing output directory before export.",
    )
    content_group = parser.add_mutually_exclusive_group()
    content_group.add_argument(
        "--content-only",
        dest="content_only",
        action="store_true",
        default=True,
        help='When using "--namespaces all", keep only content namespaces (default).',
    )
    content_group.add_argument(
        "--include-noncontent",
        dest="content_only",
        action="store_false",
        help='When using "--namespaces all", include non-content namespaces too.',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    wiki_url = args.wiki_url
    api_url = build_api_url(wiki_url, args.api_url or None)
    output_dir = Path(args.output_dir).resolve()
    if args.clean_output and output_dir.exists():
        shutil.rmtree(output_dir)
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    crawler = MediaWikiCrawler(
        wiki_url=wiki_url,
        api_url=api_url,
        output_dir=output_dir,
        timeout=args.timeout,
        min_request_interval=args.request_interval,
        keep_images=args.keep_images,
        keep_external_links=args.keep_external_links,
        extra_remove_selectors=args.remove_selector,
    )

    print(f"[info] wiki_url={wiki_url}")
    print(f"[info] api_url={api_url}")
    siteinfo = crawler.fetch_siteinfo()

    exclude_patterns = []
    if not args.no_default_exclude_titles:
        exclude_patterns.extend(DEFAULT_EXCLUDE_TITLE_PATTERNS)
    exclude_patterns.extend(args.exclude_title_regex)
    compiled_exclude_patterns = compile_title_patterns(exclude_patterns)

    max_pages = args.max_pages if args.max_pages > 0 else None
    if args.page:
        pages = crawler.list_pages_by_titles(args.page)
        namespace_ids = sorted({page.ns for page in pages})
    elif args.include_category:
        pages = crawler.list_pages_from_categories(
            category_titles=args.include_category,
            recursive_subcategories=args.category_recursive,
            max_pages=max_pages,
            exclude_title_patterns=compiled_exclude_patterns,
        )
        namespace_ids = sorted({page.ns for page in pages})
    else:
        namespace_ids = crawler.resolve_namespace_ids(args.namespaces, args.content_only)
        pages = crawler.list_pages(
            namespace_ids=namespace_ids,
            include_redirects=args.include_redirects,
            max_pages=max_pages,
            exclude_title_patterns=compiled_exclude_patterns,
        )

    if not pages:
        print("[error] no pages found")
        return 1

    print(
        f"[info] discovered pages: {len(pages)} "
        f"(namespaces={namespace_ids}, excluded_patterns={len(compiled_exclude_patterns)})"
    )
    if args.include_category and not args.page:
        print(
            f"[info] category mode: categories={args.include_category}, "
            f"recursive={args.category_recursive}"
        )
    title_to_file = build_filename_mapping(pages)
    redirects = crawler.fetch_redirect_map(pages)
    print(f"[info] redirects resolved to crawled pages: {len(redirects)}")

    ok = 0
    failed: List[str] = []
    page_categories: Dict[int, List[str]] = {}
    total = len(pages)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {
            executor.submit(write_page_markdown, crawler, page, title_to_file, pages_dir): page
            for page in pages
        }
        done = 0
        for future in as_completed(future_map):
            done += 1
            success, message, categories = future.result()
            if success:
                ok += 1
                page_categories[future_map[future].pageid] = categories
            else:
                failed.append(message)

            if done % 20 == 0 or done == total:
                print(f"[info] progress {done}/{total} (ok={ok}, failed={len(failed)})")

    write_indexes(output_dir, pages, title_to_file, siteinfo, namespace_ids, page_categories)

    print(f"[done] markdown written to: {output_dir}")
    print(f"[done] pages ok: {ok}, failed: {len(failed)}")

    if failed:
        failed_log = output_dir / "failed_pages.log"
        failed_log.write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"[warn] failures logged in: {failed_log}")

    return 0 if ok > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
