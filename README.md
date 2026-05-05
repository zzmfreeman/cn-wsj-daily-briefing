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

Output page:

- `docs/index.html`

## Env Vars

- `COOKIE_FILE`
- `PRIMARY_BASE_URL`, `PRIMARY_API_KEY`, `PRIMARY_MODEL`
- `FALLBACK_BASE_URL`, `FALLBACK_API_KEY`, `FALLBACK_MODEL`
