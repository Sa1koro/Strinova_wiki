# Changelog

格式：每个版本按「脚本」「数据」「文档」分类记录。数据更新需写明抓取日期与页数。

## [Unreleased]

### 脚本

- `mediawiki_to_markdown.py`
  - 文件名改为可读的页面标题（保留中文），重名时追加 `_<pageid>`；
    旧版 478/761 个文件退化为 `<id>_page.md`
  - 修复 `formatversion=2` 下 frontmatter `categories` 始终为空；`index.json` 增加每页分类
  - 默认删除图片、音视频、媒体文件直链、目录、面包屑、编辑/历史/上传等 UI 链接
  - 未抓取页面与站外链接默认只保留文字（`--keep-external-links` 恢复）
  - 链接改写支持重定向（`乌尔比诺` → `角色阵营.md`）
  - 修复跨行链接文字、标题前缺少空行等 Markdown 排版问题
  - frontmatter 改为合法 YAML（JSON 转义），`source` 使用规范条目 URL，正文前增加 `# 标题`
  - 新参数：`--page`、`--remove-selector`、`--keep-images`、`--keep-external-links`
  - 50 页样本测试：体积 2.39 MB → 0.90 MB（-62%），URL 字节 1.23 MB → 4 KB（仅 `source` 行）
- `merge_knowledgebase.py`：种子页面改为按标题查找，不再依赖文件名

### 其他

- `.gitignore` 增加 `.venv/`、`__pycache__/`、`.DS_Store`、`klbq_test*/`、Obsidian `workspace*.json`
- 新增 `CONTRIBUTING.md`（Git 与数据更新规范），重写 `README.md`

### 数据

- `klbq/` 仍为 2026-03-26 用旧版脚本抓取的结果（761 页），尚未用新版重新抓取

## 2026-03-26

- 首次全量抓取 `分类:内容页面`（761 页），生成 `knowledgebase/` 与 `Knowledgebase_offline/`
