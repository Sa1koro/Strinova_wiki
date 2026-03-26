#!/usr/bin/env python3
"""
Clean merged markdown knowledgebase files for offline embedding.

Goals:
- Remove external web URLs (http/https)
- Remove local markdown file links like (./26729_page.md)
- Keep readable link text only
- Output exactly the 7 merged KB files into another folder
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from typing import List


GROUP_FILE_RE = re.compile(r"^\d{2}_.+\.md$")

# ![alt](url) -> alt
IMAGE_LINK_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
# [text](url "title") -> text
INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# <https://example.com> -> ""
ANGLE_URL_RE = re.compile(r"<https?://[^>\s]+>")
# plain https://example.com -> ""
PLAIN_URL_RE = re.compile(r"https?://[^\s)]+")


def clean_markdown(text: str) -> str:
    out = text.replace("\r\n", "\n")

    # Drop link destinations while keeping visible text.
    out = IMAGE_LINK_RE.sub(lambda m: (m.group(1) or "").strip(), out)
    out = INLINE_LINK_RE.sub(lambda m: m.group(1), out)

    # Remove leftover URL text.
    out = ANGLE_URL_RE.sub("", out)
    out = PLAIN_URL_RE.sub("", out)

    # Remove empty markdown artifacts.
    out = re.sub(r"\[\s*]\(\s*\)", "", out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"


def discover_group_files(input_dir: Path) -> List[Path]:
    files = [p for p in input_dir.iterdir() if p.is_file() and GROUP_FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean merged knowledgebase markdown for offline embedding.")
    parser.add_argument("--input-dir", default="knowledgebase", help="Directory containing merged KB markdown files.")
    parser.add_argument("--output-dir", default="Knowledgebase_offline", help="Directory for cleaned offline files.")
    parser.add_argument(
        "--clear-output",
        action="store_true",
        help="Delete output directory before writing cleaned files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not input_dir.exists():
        raise FileNotFoundError(f"input dir not found: {input_dir}")

    if args.clear_output and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    group_files = discover_group_files(input_dir)
    if len(group_files) != 7:
        print(f"[warn] expected 7 group files, found {len(group_files)}")

    for src in group_files:
        raw = src.read_text(encoding="utf-8", errors="replace")
        cleaned = clean_markdown(raw)
        dst = output_dir / src.name
        dst.write_text(cleaned, encoding="utf-8")
        print(f"[cleaned] {src.name}")

    print(f"[done] output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
