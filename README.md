# cn-wsj-daily-briefing

Daily auto-briefing for `cn.wsj.com` with WSJ-like web presentation.

## Features

- Discovers article URLs from **homepage** and/or **RSS/Atom feeds**（`WSJ_RSS_URLS`）
- Filters to a configurable time window (default **24h**, Beijing)：`BRIEF_WINDOW_HOURS`（最大 14 天，用于 RSS 延迟等情况）
- Fetches cn.wsj.com article pages when reachable；若站点拦截则用 **RSS/HTML 片段兜底** 生成摘要素材
- Extracts original title, lede, hero image
- Generates AI summaries with primary/fallback model strategy
- Renders a WSJ-like static page for publishing
- Designed for GitHub Pages delivery

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 推荐：DataDome 拦首页时，改用 RSS 发现 + 可选正文兜底
export ARTICLE_DISCOVERY="both"   # homepage | rss | both
# 可选：不填时会使用内置官方 WSJ feeds（Markets/World/Tech/Business）
export WSJ_RSS_URLS="https://example.com/your-feed.xml"  # 逗号分隔多个

export COOKIE_FILE="/absolute/path/to/cn.wsj.com_netscape_xxx.txt"   # 可选
python run.py
```

Output:

- Portal（指向最新一次）：`docs/index.html`
- 每次带时间戳的版本：`docs/runs/YYYY-MM-DD_HH-MM-SS/index.html`

每次运行会抓取「当前时刻往回 24 小时内」且能解析到正文的文章；**不会在多次运行之间永久跳过已处理 URL**，以免第二次只剩极少数「新」链接。

## Env Vars

- `ARTICLE_DISCOVERY`：`homepage` | `rss` | `both`（默认 `both`）。`rss` 需配置 `WSJ_RSS_URLS`。
- `BRIEF_WINDOW_HOURS`：汇入简报的时间窗口（默认 `24`，最大 `336`，即两周）。
- `WSJ_RSS_URLS`（或 `RSS_URLS`）：逗号分隔的 RSS/Atom 地址。不填时默认使用官方 WSJ feeds（`feeds.a.dj.com`）。
- `ALLOW_UNDATED_ARTICLES`：是否允许“无发布时间”文章兜底入选（默认 `1`）。
- `MAX_UNDATED_ARTICLES`：无发布时间兜底最多纳入条数（默认 `5`）。
- `COOKIE_FILE`
- `PRIMARY_BASE_URL`, `PRIMARY_API_KEY`, `PRIMARY_MODEL`
- `FALLBACK_BASE_URL`, `FALLBACK_API_KEY`, `FALLBACK_MODEL`

未配置上述模型变量时，页面使用「正文摘录」生成摘要，**情绪一栏会显示未判定**；配置后即可输出模型摘要与偏多/中性/偏空判断。

### 反爬 / DataDome（首页 401、0 篇文章）

运行时若日志里出现 `captcha-delivery.com` / `geo.captcha-delivery.com`，说明站点返回的是 **验证码/反爬挑战**，不是「24 小时过滤」问题；仅靠 Netscape Cookie 往往不够。

推荐做法（一次登录，长期复用）：

1. `source .venv/bin/activate`
2. `python scripts/save_cn_wsj_storage_state.py --wait-seconds 180`（会打开 Chrome；脚本在超时后自动写入 JSON，终端 **无需按回车**）
3. `export PLAYWRIGHT_STORAGE_STATE="/path/to/cn_wsj_storage.json"`
4. 仍可同时保留 `COOKIE_FILE`；抓取会优先使用 `storage_state`。

### RSS-only 流水线（尽量不碰首页）

当 `PLAYWRIGHT_STORAGE_STATE`/Cookie 都失效时，`cn.wsj.com` 正文与首页可能仍不可用。可按合规来源配置 **可用的** RSS，`ARTICLE_DISCOVERY=rss`，脚本会用 feed 内的 HTML/摘要拼正文并继续出简报。**第三方聚合 feed 可能存在滞后、删减或停运，请以你能稳定访问的订阅源为准。**

可选：`PLAYWRIGHT_USE_CHROME_CHANNEL=0` 可强制使用 Playwright 自带 Chromium（默认尝试系统 Chrome）。
