# cn-wsj-daily-briefing

Daily auto-briefing for `cn.wsj.com` with WSJ-like web presentation.

## Features

- Fetches cn.wsj.com homepage and article pages
- Filters to last 24 hours in Beijing time
- Extracts original title, lede, hero image
- Generates AI summaries with primary/fallback model strategy
- Renders a WSJ-like static page for publishing
- Designed for GitHub Pages delivery

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export COOKIE_FILE="/absolute/path/to/cn.wsj.com_netscape_xxx.txt"
python run.py
```

Output:

- Portal（指向最新一次）：`docs/index.html`
- 每次带时间戳的版本：`docs/runs/YYYY-MM-DD_HH-MM-SS/index.html`

每次运行会抓取「当前时刻往回 24 小时内」且能解析到正文的文章；**不会在多次运行之间永久跳过已处理 URL**，以免第二次只剩极少数「新」链接。

## Env Vars

- `COOKIE_FILE`
- `PRIMARY_BASE_URL`, `PRIMARY_API_KEY`, `PRIMARY_MODEL`
- `FALLBACK_BASE_URL`, `FALLBACK_API_KEY`, `FALLBACK_MODEL`

未配置上述模型变量时，页面使用「正文摘录」生成摘要，**情绪一栏会显示未判定**；配置后即可输出模型摘要与偏多/中性/偏空判断。
