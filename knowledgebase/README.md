# Knowledgebase Bundle

- 源页面总数: **761**
- 合并后文件数: **7**

## 文件列表

- `01_announcements.md`: 163 pages
- `02_characters.md`: 62 pages
- `03_story.md`: 112 pages
- `04_maps.md`: 28 pages
- `05_events.md`: 1 pages
- `06_weapons.md`: 40 pages
- `07_others.md`: 355 pages

```
python3 scripts/merge_knowledgebase.py \
  --index-file klbq/index.json \
  --pages-dir klbq/pages \
  --output-dir knowledgebase \
  --max-depth 1
```