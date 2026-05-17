#!/bin/bash
# 投资工作室H5 — 自动刷新行情并推送
# 交易日 9:25-16:00 每5分钟自动执行（AKShare免费源，无调用限制）
# 用法: chmod +x auto-refresh.sh && ./auto-refresh.sh

set -e
cd "$(dirname "$0")"

# 检查是否交易日+交易时间
DOW=$(date +%u)  # 1=Mon, 7=Sun
HOUR=$(date +%H)
MIN=$(date +%M)
HHMM="${HOUR}${MIN}"

if [ "$DOW" -gt 5 ]; then
    echo "$(date): 周末，跳过"
    exit 0
fi

# A股: 9:25-11:30, 13:00-15:05 | 港股: 9:30-16:00
# 取并集: 9:25-16:00
if [ "$HHMM" -lt "0925" ] || [ "$HHMM" -gt "1600" ]; then
    echo "$(date): 非交易时间，跳过"
    exit 0
fi

echo "$(date): 开始刷新行情（AKShare自动模式）..."

# 绕过代理（eastmoney.com不需要翻墙，走直连更快）
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

# 备��Key（auto模式不使用，手动模式需要）
export MX_APIKEY="mkt_f-JSym1MjVyEBaoal60UkgLwEd69FhteaSCakjQE8Ic"

# 刷新数据 — 默认auto模式，使用AKShare免费源
python3 refresh-data.py

# 推送到GitHub（Cloudflare Pages自动部署）
git add data.json
git diff --cached --quiet || {
    git commit -m "📈 $(date +%H:%M) 行情更新"
    git push origin main
    echo "$(date): 已推送到GitHub"
}

echo "$(date): 完成"
