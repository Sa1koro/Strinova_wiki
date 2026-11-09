# 开发与维护规范

## Git 工作流

- `main` 始终保持可用：脚本能运行、`klbq/` 是完整的一次抓取结果
- 所有改动在分支上进行，确认后再合并到 `main`

| 分支前缀 | 用途 | 示例 |
| --- | --- | --- |
| `feat/` | 新功能 | `feat/page-filter` |
| `fix/` | 修 bug | `fix/category-frontmatter` |
| `data/` | 只更新抓取数据 | `data/2026-11-09` |
| `docs/` | 只改文档 | `docs/readme-usage` |

### 提交信息

格式：`<类型>: <简要说明>`，说明用中文或英文均可，一次提交只做一件事。

| 类型 | 用途 |
| --- | --- |
| `feat` | 新功能 / 新参数 |
| `fix` | 修复错误 |
| `refactor` | 不改变行为的代码调整 |
| `docs` | 文档 |
| `data` | 重新抓取 / 更新知识库数据 |
| `chore` | 依赖、`.gitignore` 等杂项 |

```text
feat: 新增 --page 参数按标题抓取
fix: 修复 formatversion=2 下分类为空
data: 全量重新抓取 klbq（761 页）
```

**脚本改动和数据更新分开提交**：数据提交往往有几百个文件，混在一起很难审查代码 diff。

### 不提交的内容

见 `.gitignore`：`.venv/`、`__pycache__/`、`.DS_Store`、`klbq_test*/`、
Obsidian 的 `workspace*.json`（个人界面状态）、`klbq_00/`（旧版抓取结果）。

---

## 修改脚本

1. 新建分支
2. 修改 `scripts/` 下的脚本
3. 语法检查：`.venv/bin/python -m py_compile scripts/*.py`
4. 小规模试抓，对比前后效果：

   ```bash
   .venv/bin/python scripts/mediawiki_to_markdown.py \
     --wiki-url "https://wiki.biligame.com/klbq/" \
     --output-dir klbq_test --clean-output \
     --page "星绘" --page "星绘/档案馆" --page "星绘/语音台词" --page "北极星"
   ```

5. 检查清单：
   - [ ] 文件名可读，没有大量 `page.md` / `_1.md` 之类的退化文件名
   - [ ] frontmatter 中 `categories` 不为空
   - [ ] 正文中没有残留的 `https://`、`.png`、`.mp3`、`媒体文件:`
   - [ ] `](./xxx.md)` 链接指向的文件确实存在
   - [ ] 表格、列表、标题层级没有错乱

   前三项可以用下面的命令快速检查：

   ```bash
   grep -L 'categories: \["' klbq_test/pages/*.md          # 分类为空的页面
   grep -c 'https\?://\|\.png\|\.mp3' klbq_test/pages/*.md  # 每页残留数（source 行占 1）
   ```

6. 更新 `README.md`（参数、清洗规则）和 `CHANGELOG.md`
7. 提交、合并

### 代码约定

- 只依赖 `requirements.txt` 中的库
- 站点专有的清洗规则尽量通过 `--remove-selector` 参数传入，脚本内只放通用的 MediaWiki 规则
- 修改 `mediawiki_to_markdown.py` 的输出格式（文件名、frontmatter、`index.json`）时，
  同步检查 `merge_knowledgebase.py`；合并脚本通过 **页面标题** 查找种子页面，不要写死文件名

---

## 更新知识库数据

1. 新建分支 `data/<日期>`
2. 抓到 **新目录**，不要直接覆盖 `klbq/`：

   ```bash
   .venv/bin/python scripts/mediawiki_to_markdown.py \
     --wiki-url "https://wiki.biligame.com/klbq/" \
     --output-dir klbq_new \
     --include-category "分类:内容页面" \
     --no-default-exclude-titles \
     --workers 1 --request-interval 0.35
   ```

3. 检查 `klbq_new/failed_pages.log`，失败页用 `--page` 补抓
4. 确认无误后替换（保留 Obsidian 配置）：

   ```bash
   rm -rf klbq/pages klbq/index.json klbq/README.md
   mv klbq_new/pages klbq_new/index.json klbq_new/README.md klbq/
   rm -rf klbq_new
   ```

   必须整体替换 `pages/`：文件名规则变化后，旧文件不会被自动覆盖，会残留下来。

5. 如果使用合并版知识库，重新生成：

   ```bash
   .venv/bin/python scripts/merge_knowledgebase.py
   .venv/bin/python scripts/clean_offline_knowledgebase.py
   ```

6. 提交：`data: 全量重新抓取 klbq（N 页）`，并在 `CHANGELOG.md` 记录日期和页数
