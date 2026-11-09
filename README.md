# Strinova Wiki 知识库

把 [卡拉彼丘 WIKI](https://wiki.biligame.com/klbq/)（MediaWiki 站点）抓取并清洗成
互相链接的 Markdown 文件，作为 AstrBot 人格（如「星绘」）的嵌入式知识库使用。

人物卡只保留角色核心设定，世界观等大量内容交给知识库按需检索，以节省 token。

---

## 目录结构

```text
.
├── scripts/
│   ├── mediawiki_to_markdown.py        # 通用 MediaWiki 抓取 + 清洗（主脚本）
│   ├── merge_knowledgebase.py          # 可选：按主题把页面合并成 7 个大文件
│   └── clean_offline_knowledgebase.py  # 可选：去掉合并文件里的链接/URL
├── klbq/                               # 抓取结果（推荐直接作为知识库）
│   ├── pages/<页面标题>.md
│   ├── index.json                      # 全部页面元数据（标题/分类/文件名）
│   └── README.md
├── knowledgebase/                      # merge_knowledgebase.py 的输出
├── Knowledgebase_offline/              # clean_offline_knowledgebase.py 的输出
├── CONTRIBUTING.md                     # 开发 / Git / 数据更新规范
├── CHANGELOG.md
└── requirements.txt
```

---

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 2. 小规模试抓（推荐先做）

```bash
.venv/bin/python scripts/mediawiki_to_markdown.py \
  --wiki-url "https://wiki.biligame.com/klbq/" \
  --output-dir klbq_test \
  --include-category "分类:内容页面" \
  --no-default-exclude-titles \
  --max-pages 50
```

只看指定页面（会自动跟随重定向）：

```bash
.venv/bin/python scripts/mediawiki_to_markdown.py \
  --wiki-url "https://wiki.biligame.com/klbq/" \
  --output-dir klbq_test \
  --page "星绘" --page "星绘/档案馆" --page "弦化"
```

`klbq_test*/` 已在 `.gitignore` 中，不会被提交。

### 3. 全量抓取

```bash
.venv/bin/python scripts/mediawiki_to_markdown.py \
  --wiki-url "https://wiki.biligame.com/klbq/" \
  --output-dir klbq_new \
  --include-category "分类:内容页面" \
  --no-default-exclude-titles \
  --workers 1 \
  --request-interval 0.35
```

确认 `klbq_new/` 无误后再替换 `klbq/`（见 [CONTRIBUTING.md](CONTRIBUTING.md#更新知识库数据)）。

---

## 输出格式

文件名直接使用页面标题（`/`、`:`、空格等不安全字符替换为 `_`），
与其他页面重名（不区分大小写）时追加 `_<pageid>`。

```markdown
---
title: "星绘/档案馆"
pageid: 10438
namespace: 0
source: https://wiki.biligame.com/klbq/%E6%98%9F%E7%BB%98/%E6%A1%A3%E6%A1%88%E9%A6%86
categories: ["档案馆"]
---

# 星绘/档案馆

- [星绘](./星绘.md)
- [武器](./北极星.md)
...
```

- 指向已抓取页面的链接（包括经由重定向的）改写为 `./<文件>.md`
- 指向未抓取页面 / 站外的链接只保留文字（`--keep-external-links` 可保留 URL）
- `index.json` 中每页都带 `categories`，可用于分组或过滤

### 默认清洗规则

| 内容 | 处理 |
| --- | --- |
| `<script>` / `<style>` / 导航框 / 编辑按钮 | 删除 |
| 目录（TOC）、面包屑 | 删除 |
| 图片、音视频、媒体文件直链（`a.internal`） | 删除（`--keep-images` 保留图片） |
| 编辑 / 历史 / 刷新 / 短链接 / 上传文件等 UI 链接 | 删除 |
| 参考文献角标与列表 | 删除 |
| 页内锚点链接、自链接 | 只保留文字 |

站点改版后如出现新的噪声块，用 `--remove-selector "<CSS 选择器>"` 追加，无需改代码。

---

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--wiki-url` | 站点根 URL（必填） |
| `--api-url` | API 地址，默认 `<wiki-url>/api.php` |
| `--output-dir` | 输出目录，默认 `klbq` |
| `--page` | 只抓指定标题，可重复；优先于分类模式 |
| `--include-category` | 从分类开始抓，可重复；默认递归子分类 |
| `--no-category-recursive` | 分类模式下不递归子分类 |
| `--namespaces` | 全站模式的命名空间 ID，逗号分隔或 `all` |
| `--exclude-title-regex` | 按标题排除页面，可重复 |
| `--no-default-exclude-titles` | 关闭内置的 `分类:` / `模板:` / `帮助:` 等排除规则 |
| `--remove-selector` | 额外删除的 CSS 选择器，可重复 |
| `--keep-images` / `--keep-external-links` | 保留图片 / 外链 URL |
| `--max-pages` | 最多抓取页数（测试用） |
| `--workers` / `--request-interval` | 并发数 / 最小请求间隔（秒） |
| `--clean-output` | **先删除整个输出目录**再抓取，谨慎使用 |

完整列表：`.venv/bin/python scripts/mediawiki_to_markdown.py --help`

---

## 用于 AstrBot 知识库

- 推荐直接导入 `klbq/pages/*.md`：一页一个文件，检索命中时来源清晰
- 按需排除对人格没用、但体积很大的页面，例如：

  ```bash
  --exclude-title-regex ".*/(画廊|语音台词)$" \
  --exclude-title-regex ".*PV$"
  ```

- 人物卡只放星绘的核心设定（`星绘.md` 中的档案信息 + 性格），
  世界观、阵营、剧情交给知识库检索

---

## 常见问题

**`567 Server Error`**：站点风控。保持 `--workers 1`，把 `--request-interval` 调到 `0.5 ~ 1.0`，稍后重试。

**`ProxyError ... 403 Forbidden`**：网络 / 代理问题，检查 `HTTP_PROXY` / `HTTPS_PROXY` 或换网络。

**失败的页面**：记录在 `<output-dir>/failed_pages.log`，可以用 `--page` 单独补抓。
