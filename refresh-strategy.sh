#!/bin/bash
# 收盘后刷新 strategy.json 并部署到 CF Pages(invest-cake）。
# 由 openclaw cron(本机)调用。绝不在沙箱跑——沙箱 akshare 被拦会产空数据。
# 防呆(R1 教训):仅当 mode=live 且 stockCards/chainTree 非空才 git push,
#                否则中止、不污染线上看板。
set -euo pipefail
cd "$HOME/.openclaw/投资工作室H5"

python3 strategy-feed.py            # 本机 live 数据(无 --dry-run 即 live)
python3 enrich-strategy-market.py   # 公开行情/PE/市值增强；凭证只走环境变量，不落盘

# 非空 + live + 结构字段校验:挡住"live 但数据拉空"的伪 live
if ! python3 validate-public-data.py --scope strategy
then
    echo "[abort] strategy.json 非 live 或数据为空,不部署(避免推空看板)" >&2
    exit 1
fi

git add strategy.json
git diff --cached --quiet || {
    git commit -m "🔄 strategy 收盘刷新 $(date '+%F %H:%M')"
    git push origin main
}
