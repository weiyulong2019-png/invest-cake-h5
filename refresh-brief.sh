#!/bin/bash
# 收盘后刷新 daily_brief.json 并部署到 CF Pages。
# 由 openclaw cron(本机)调用。绝不在沙箱跑。
# 防呆:仅当 date=今天 且 available 且 items 非空才 git push。
set -euo pipefail
cd "$HOME/.openclaw/投资工作室H5"

python3 daily-brief.py

if ! python3 - <<'PY'
import json, datetime, sys
d = json.load(open("daily_brief.json"))
ok = (d.get("date") == datetime.date.today().isoformat()
      and d.get("available") is True
      and len(d.get("items") or []) > 0)
sys.exit(0 if ok else 1)
PY
then
    echo "[abort] daily_brief.json 非今日或为空,不部署" >&2
    exit 1
fi

git add daily_brief.json
git diff --cached --quiet || {
    git commit -m "🔄 daily_brief 收盘刷新 $(date '+%F %H:%M')"
    git push origin main
}
