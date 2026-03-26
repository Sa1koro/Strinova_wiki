#!/usr/bin/env python3
"""
Merge exported MediaWiki markdown pages into a small number of KB files.

Default grouping roots (seed pages):
- 公告: 387_page.md
- 角色: 83_page.md
- 剧情故事: 25561_page.md
- 地图: 92_page.md
- 活动: 1499_page.md
- 武器: 444_page.md

Remaining pages are grouped into "其他".
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse


WIKI_HOST = "wiki.biligame.com"
WIKI_PATH_PREFIX = "/klbq/"

MD_LINK_RE = re.compile(r"\[[^\]]*]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)")


@dataclass
class PageRecord:
    title: str
    pageid: int
    namespace: int
    file_rel: str  # e.g. pages/387_page.md


def normalize_title(title: str) -> str:
    t = unquote(title or "").replace("_", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text

    block = text[4:end]
    body = text[end + 5 :]
    meta: Dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def load_index(index_file: Path) -> Tuple[List[PageRecord], Dict[str, PageRecord], Dict[str, str]]:
    data = json.loads(index_file.read_text(encoding="utf-8"))
    pages = data.get("pages", [])

    records: List[PageRecord] = []
    by_file: Dict[str, PageRecord] = {}
    title_to_file: Dict[str, str] = {}

    for item in pages:
        rec = PageRecord(
            title=str(item.get("title", "")),
            pageid=int(item.get("pageid", 0)),
            namespace=int(item.get("namespace", 0)),
            file_rel=str(item.get("file", "")),
        )
        if not rec.file_rel:
            continue
        records.append(rec)
        by_file[rec.file_rel] = rec
        title_to_file[normalize_title(rec.title)] = rec.file_rel

    return records, by_file, title_to_file


def resolve_target_to_file_rel(
    target: str,
    title_hint: Optional[str],
    title_to_file: Dict[str, str],
) -> Optional[str]:
    target = (target or "").strip()
    if not target:
        return None

    # Local markdown links
    if target.startswith("./") and target.endswith(".md"):
        return f"pages/{target[2:]}"
    if target.endswith(".md") and not target.startswith("http"):
        return f"pages/{target}"

    # Absolute wiki links -> map by title
    if target.startswith("http://") or target.startswith("https://"):
        parsed = urlparse(target)
        if parsed.netloc == WIKI_HOST:
            query = parse_qs(parsed.query)
            title_val = ""
            if "title" in query and query["title"]:
                title_val = normalize_title(query["title"][0])
            elif parsed.path.startswith(WIKI_PATH_PREFIX):
                title_val = normalize_title(parsed.path[len(WIKI_PATH_PREFIX) :])
            if title_val and title_val in title_to_file:
                return title_to_file[title_val]

    # Fallback: markdown title attribute
    if title_hint:
        hint = normalize_title(title_hint)
        if hint in title_to_file:
            return title_to_file[hint]

    return None


def extract_linked_files(
    md_text: str,
    title_to_file: Dict[str, str],
    valid_files: Set[str],
) -> List[str]:
    found: List[str] = []
    seen: Set[str] = set()

    for match in MD_LINK_RE.finditer(md_text):
        target = match.group(1)
        title_hint = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        file_rel = resolve_target_to_file_rel(target, title_hint, title_to_file)
        if not file_rel:
            continue
        if file_rel not in valid_files:
            continue
        if file_rel in seen:
            continue
        seen.add(file_rel)
        found.append(file_rel)
    return found


def collect_group_files(
    seed_file_rel: str,
    pages_dir: Path,
    title_to_file: Dict[str, str],
    valid_files: Set[str],
    max_depth: int,
) -> List[str]:
    if seed_file_rel not in valid_files:
        raise FileNotFoundError(f"seed not in index: {seed_file_rel}")

    ordered: List[str] = []
    discovered: Set[str] = set()
    queue = deque([(seed_file_rel, 0)])

    while queue:
        file_rel, depth = queue.popleft()
        if file_rel in discovered:
            continue
        discovered.add(file_rel)
        ordered.append(file_rel)

        if depth >= max_depth:
            continue

        file_abs = pages_dir / Path(file_rel).name
        if not file_abs.exists():
            continue
        text = file_abs.read_text(encoding="utf-8", errors="replace")
        links = extract_linked_files(text, title_to_file, valid_files)
        for nxt in links:
            if nxt not in discovered:
                queue.append((nxt, depth + 1))

    return ordered


def render_merged_markdown(
    group_name: str,
    file_rels: List[str],
    by_file: Dict[str, PageRecord],
    pages_dir: Path,
) -> str:
    lines: List[str] = []
    lines.append(f"# {group_name}")
    lines.append("")
    lines.append(f"- 页面数: **{len(file_rels)}**")
    lines.append("")
    lines.append("## 页面清单")
    lines.append("")
    for f in file_rels:
        rec = by_file[f]
        lines.append(f"- {rec.title} (`{Path(f).name}`)")
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, f in enumerate(file_rels, start=1):
        rec = by_file[f]
        page_file = pages_dir / Path(f).name
        if not page_file.exists():
            continue
        raw = page_file.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(raw)
        source = meta.get("source", "")

        lines.append(f"## {idx}. {rec.title}")
        lines.append("")
        lines.append(f"- pageid: `{rec.pageid}`")
        lines.append(f"- namespace: `{rec.namespace}`")
        if source:
            lines.append(f"- source: {source}")
        lines.append("")
        lines.append(body.strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge klbq markdown pages into KB bundles.")
    parser.add_argument("--index-file", default="klbq/index.json", help="Path to export index.json")
    parser.add_argument("--pages-dir", default="klbq/pages", help="Directory of page markdown files")
    parser.add_argument("--output-dir", default="knowledgebase", help="Output folder for merged files")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=1,
        help="Link expansion depth from each seed page (default: 1, i.e. seed + direct links).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    index_file = Path(args.index_file).resolve()
    pages_dir = Path(args.pages_dir).resolve()
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    records, by_file, title_to_file = load_index(index_file)
    valid_files = set(by_file.keys())

    groups = [
        ("公告", "01_announcements.md", "pages/387_page.md"),
        ("角色", "02_characters.md", "pages/83_page.md"),
        ("剧情故事", "03_story.md", "pages/25561_page.md"),
        ("地图", "04_maps.md", "pages/92_page.md"),
        ("活动", "05_events.md", "pages/1499_page.md"),
        ("武器", "06_weapons.md", "pages/444_page.md"),
    ]

    assigned: Set[str] = set()
    group_files: Dict[str, List[str]] = {}
    manifest: Dict[str, object] = {
        "total_pages_in_index": len(records),
        "groups": {},
    }

    for group_name, out_name, seed_rel in groups:
        collected = collect_group_files(
            seed_file_rel=seed_rel,
            pages_dir=pages_dir,
            title_to_file=title_to_file,
            valid_files=valid_files,
            max_depth=max(0, args.max_depth),
        )
        unique_for_group = [f for f in collected if f not in assigned]
        assigned.update(unique_for_group)
        group_files[group_name] = unique_for_group

        merged = render_merged_markdown(group_name, unique_for_group, by_file, pages_dir)
        (out_dir / out_name).write_text(merged, encoding="utf-8")

        manifest["groups"][group_name] = {
            "seed_file": seed_rel,
            "output_file": out_name,
            "count": len(unique_for_group),
        }

    # 其他 = 剩余未分配页面
    remaining = [rec.file_rel for rec in records if rec.file_rel not in assigned]
    remaining_sorted = sorted(remaining, key=lambda f: (by_file[f].namespace, by_file[f].title.lower()))
    group_files["其他"] = remaining_sorted

    merged_other = render_merged_markdown("其他", remaining_sorted, by_file, pages_dir)
    (out_dir / "07_others.md").write_text(merged_other, encoding="utf-8")
    manifest["groups"]["其他"] = {
        "seed_file": None,
        "output_file": "07_others.md",
        "count": len(remaining_sorted),
    }

    manifest["total_assigned"] = sum(v["count"] for v in manifest["groups"].values())
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    quick = [
        "# Knowledgebase Bundle",
        "",
        f"- 源页面总数: **{manifest['total_pages_in_index']}**",
        f"- 合并后文件数: **{len(manifest['groups'])}**",
        "",
        "## 文件列表",
        "",
    ]
    for g in ["公告", "角色", "剧情故事", "地图", "活动", "武器", "其他"]:
        info = manifest["groups"][g]
        quick.append(f"- `{info['output_file']}`: {info['count']} pages")

    (out_dir / "README.md").write_text("\n".join(quick) + "\n", encoding="utf-8")

    print(f"[done] output_dir={out_dir}")
    for g in ["公告", "角色", "剧情故事", "地图", "活动", "武器", "其他"]:
        info = manifest["groups"][g]
        print(f"[group] {g}: {info['count']} -> {info['output_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
