# MediaWiki -> Markdown 手动抓取指南（Strinova / klbq）

这个仓库里的 `scripts/mediawiki_to_markdown.py` 是一个通用 MediaWiki 抓取脚本。  
目标是把站点页面抓取并清洗成可互相链接的 Markdown 文件，输出到 `klbq/`。

适用目标站点示例：`https://wiki.biligame.com/klbq/`

---

## 1) 脚本会做什么

- 通过 MediaWiki API 枚举页面
- 获取页面解析后的 HTML 内容
- 清理常见噪声（脚本、编辑按钮、引用回链等）
- 转成 Markdown
- 将站内链接改写为本地 `./xxx.md` 链接
- 生成索引文件（`index.json`）与示例入口（`README.md`）

---

## 2) 输出目录结构

默认输出目录是 `klbq/`，典型结构如下：

```text
klbq/
  pages/
    <pageid>_<slug>.md
  index.json
  failed_pages.log   # 若有失败页则出现
  README.md
```

---

## 3) 先安装依赖（手动执行）

建议使用项目虚拟环境，不污染系统 Python：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

---

## 4) 按「分类:内容页面」抓取（你当前需求）

> 这条命令 **不会清空已存在目录**，只会在 `klbq/` 内写入/覆盖同名文件。  
> 只抓 `分类:内容页面` 及其子分类里的页面，适合你给的人格知识库场景。

```bash
.venv/bin/python scripts/mediawiki_to_markdown.py \
  --wiki-url "https://wiki.biligame.com/klbq/" \
  --output-dir klbq \
  --include-category "分类:内容页面" \
  --category-recursive \
  --no-default-exclude-titles \
  --workers 1 \
  --request-interval 0.35
```

说明：

- `--include-category`：从指定分类开始抓
- `--category-recursive`：递归抓子分类（你列的那一长串子分类会自动覆盖）
- `--no-default-exclude-titles`：关闭内置排除，避免把你需要的 `帮助:` 页面也排掉
- `--workers 1` + `--request-interval`：降低并发与请求频率，减少风控/567 失败

如果你只想抓其中几个子分类，可重复写 `--include-category`：

```bash
--include-category "分类:角色" \
--include-category "分类:剧情故事" \
--include-category "分类:武器"
```

---

## 5) 先小规模测试（推荐）

先抓 50 页验证格式与过滤是否满意：

```bash
.venv/bin/python scripts/mediawiki_to_markdown.py \
  --wiki-url "https://wiki.biligame.com/klbq/" \
  --output-dir klbq_test \
  --include-category "分类:内容页面" \
  --category-recursive \
  --no-default-exclude-titles \
  --workers 1 \
  --request-interval 0.35 \
  --max-pages 50
```

确认没问题后再跑全量。

---

## 6) 关于 `--clean-output`（危险参数）

脚本支持 `--clean-output`，行为是：

- 如果输出目录存在，先整目录删除，再重新抓取

请只在你明确备份完成时再用。  
如果不想冒风险，**不要加这个参数**。

---

## 7) 常见问题排查

### A. `567 Server Error`

通常是站点风控或瞬时拒绝。可尝试：

- 保持 `--workers 1`
- 将 `--request-interval` 提高到 `0.5 ~ 1.0`
- 过一段时间后重试

### B. `ProxyError ... Tunnel connection failed: 403 Forbidden`

通常是当前网络/代理环境阻断（不是脚本逻辑错误）。可尝试：

- 切换网络（如手机热点）
- 检查系统代理或终端代理变量（`HTTP_PROXY` / `HTTPS_PROXY`）
- 在无代理或可直连环境重跑

### C. 输出里仍出现少量不需要页面

继续追加排除规则即可，例如：

```bash
--exclude-title-regex "^文件:.*" \
--exclude-title-regex "^分类:.*" \
--exclude-title-regex "^模板:.*"
```

如果你已经使用了 `--include-category "分类:内容页面"`，优先先检查是否误加了
`--include-noncontent` 或其他额外分类。

---

## 8) 给 AstrBot 人格知识库的建议（节省 token）

- 不要把全站都进主提示词
- 让模型先检索 `klbq/pages/*.md`，只注入命中的片段
- 针对角色（如星绘）优先保留：
  - 角色页
  - 剧情/设定页
  - 世界观/术语页
  - 版本活动中与角色相关的公告页

---

## 9) 目标站点参考

- [卡拉彼丘WIKI](https://wiki.biligame.com/klbq/)
- [分类:内容页面](https://wiki.biligame.com/klbq/%E5%88%86%E7%B1%BB:%E5%86%85%E5%AE%B9%E9%A1%B5%E9%9D%A2)

