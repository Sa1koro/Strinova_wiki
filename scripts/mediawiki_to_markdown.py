#!/usr/bin/env python3
"""
Generic MediaWiki crawler:
1) Enumerates pages from MediaWiki API
2) Parses page HTML
3) Cleans noisy wiki-specific nodes
4) Converts HTML to Markdown
5) Rewrites internal links to local Markdown files
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md


REMOVE_SELECTORS = (
    "script",
    "style",
    "noscript",
    ".mw-editsection",
    ".mw-cite-backlink",
    ".catlinks",
    ".printfooter",
    ".mw-authority-control",
    ".metadata",
    "table.navbox",
    "div.navbox",
    "table.vertical-navbox",
    "div.hatnote.navigation-not-searchable",
)


def normalize_title(title: str) -> str:
    title = unquote(title or "")
    title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def safe_stem_from_title(title: str, max_len: int = 80) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title.strip())
    stem = re.sub(r"_+", "_", stem).strip("._")
    if not stem:
        stem = "page"
    return stem[:max_len]


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
        user_agent: str = "mediawiki-to-markdown-bot/1.0",
    ) -> None:
        self.wiki_url = wiki_url.rstrip("/") + "/"
        self.api_url = api_url
        self.output_dir = output_dir
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent

        parsed = urlparse(self.wiki_url)
        self.root_scheme = parsed.scheme
        self.root_host = parsed.netloc

        self.siteinfo = {}
        self.article_prefix = "/"
        self.script_path = "/index.php"

    def api_get(self, params: Dict[str, object]) -> Dict[str, object]:
        payload = dict(params)
        payload.setdefault("format", "json")
        payload.setdefault("formatversion", 2)
        payload.setdefault("utf8", 1)

        headers = {"User-Agent": self.user_agent}

        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    self.api_url, params=payload, headers=headers, timeout=self.timeout
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
                "siprop": "general|namespaces",
            }
        )
        query = data.get("query", {})
        general = query.get("general", {}) or {}
        namespaces = query.get("namespaces", {}) or {}

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
        return self.siteinfo

    def resolve_namespace_ids(self, namespaces_arg: str) -> List[int]:
        if not self.siteinfo:
            self.fetch_siteinfo()
        namespaces = self.siteinfo["namespaces"]

        all_ns = sorted(int(key) for key in namespaces.keys())
        if namespaces_arg.lower() == "all":
            return [ns for ns in all_ns if ns >= 0]

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
                    pages.append(page)
                    if max_pages and len(pages) >= max_pages:
                        return pages

                if "continue" not in data:
                    break
                continuation = data["continue"]

        return pages

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
            if title:
                return normalize_title(title)

        if path == self.script_path and "title" in query and query["title"]:
            return normalize_title(query["title"][0])

        return None

    def clean_and_rewrite_html(
        self,
        html: str,
        title_to_file: Dict[str, str],
    ) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for selector in REMOVE_SELECTORS:
            for node in soup.select(selector):
                node.decompose()

        # Remove references and citation backlinks for cleaner persona KB text.
        for node in soup.select("sup.reference, span.reference, ol.references, div.reflist"):
            node.decompose()

        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                img["src"] = urljoin(self.wiki_url, src)

        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not href:
                continue

            target_title = self.extract_title_from_href(href)
            if target_title and target_title in title_to_file:
                anchor["href"] = f"./{title_to_file[target_title]}"
                continue

            if href.startswith("/"):
                anchor["href"] = urljoin(self.wiki_url, href)

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
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def build_filename_mapping(pages: List[PageInfo]) -> Dict[str, str]:
    used = set()
    mapping: Dict[str, str] = {}

    for page in pages:
        norm = normalize_title(page.title)
        stem = safe_stem_from_title(page.title)
        filename = f"{page.pageid}_{stem}.md"

        idx = 1
        while filename.lower() in used:
            filename = f"{page.pageid}_{stem}_{idx}.md"
            idx += 1

        used.add(filename.lower())
        mapping[norm] = filename

    return mapping


def write_page_markdown(
    crawler: MediaWikiCrawler,
    page: PageInfo,
    title_to_file: Dict[str, str],
    pages_dir: Path,
) -> Tuple[bool, str]:
    try:
        parsed = crawler.parse_page(page.title)
        html = parsed.get("text", "")
        if not html:
            return (False, f"empty content: {page.title}")

        cleaned_html = crawler.clean_and_rewrite_html(html, title_to_file)
        markdown_body = crawler.html_to_markdown(cleaned_html)

        categories_raw = parsed.get("categories", []) or []
        categories = []
        for item in categories_raw:
            title = item.get("title")
            if title:
                categories.append(str(title))

        source_url = urljoin(crawler.wiki_url, f"index.php?title={page.title}")
        out_file = pages_dir / title_to_file[normalize_title(page.title)]

        yaml_lines = [
            "---",
            f'title: "{page.title.replace(chr(34), chr(92) + chr(34))}"',
            f"pageid: {page.pageid}",
            f"namespace: {page.ns}",
            f"source: {source_url}",
            "categories:",
        ]
        if categories:
            yaml_lines.extend(f"  - {c}" for c in categories)
        else:
            yaml_lines.append("  -")
        yaml_lines.append("---\n")

        content = "\n".join(yaml_lines) + markdown_body
        out_file.write_text(content, encoding="utf-8")
        return (True, page.title)
    except Exception as exc:  # pylint: disable=broad-except
        return (False, f"{page.title}: {exc}")


def write_indexes(
    output_dir: Path,
    pages: List[PageInfo],
    title_to_file: Dict[str, str],
    siteinfo: Dict[str, object],
    namespace_ids: List[int],
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
                "file": f"pages/{title_to_file[normalize_title(p.title)]}",
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
        default="0",
        help='Comma-separated namespace ids, or "all". Default: 0',
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
        default=6,
        help="Concurrent workers for page parsing (default: 6).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="HTTP timeout seconds (default: 25).",
    )
    parser.add_argument(
        "--include-redirects",
        action="store_true",
        help="Include redirect pages in output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    wiki_url = args.wiki_url
    api_url = build_api_url(wiki_url, args.api_url or None)
    output_dir = Path(args.output_dir).resolve()
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    crawler = MediaWikiCrawler(
        wiki_url=wiki_url,
        api_url=api_url,
        output_dir=output_dir,
        timeout=args.timeout,
    )

    print(f"[info] wiki_url={wiki_url}")
    print(f"[info] api_url={api_url}")
    siteinfo = crawler.fetch_siteinfo()

    namespace_ids = crawler.resolve_namespace_ids(args.namespaces)
    max_pages = args.max_pages if args.max_pages > 0 else None
    pages = crawler.list_pages(
        namespace_ids=namespace_ids,
        include_redirects=args.include_redirects,
        max_pages=max_pages,
    )

    if not pages:
        print("[error] no pages found")
        return 1

    print(f"[info] discovered pages: {len(pages)}")
    title_to_file = build_filename_mapping(pages)

    ok = 0
    failed: List[str] = []
    total = len(pages)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {
            executor.submit(write_page_markdown, crawler, page, title_to_file, pages_dir): page
            for page in pages
        }
        done = 0
        for future in as_completed(future_map):
            done += 1
            success, message = future.result()
            if success:
                ok += 1
            else:
                failed.append(message)

            if done % 20 == 0 or done == total:
                print(f"[info] progress {done}/{total} (ok={ok}, failed={len(failed)})")

    write_indexes(output_dir, pages, title_to_file, siteinfo, namespace_ids)

    print(f"[done] markdown written to: {output_dir}")
    print(f"[done] pages ok: {ok}, failed: {len(failed)}")

    if failed:
        failed_log = output_dir / "failed_pages.log"
        failed_log.write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"[warn] failures logged in: {failed_log}")

    return 0 if ok > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
