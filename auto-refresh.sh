#!/bin/bash
# OpenClaw投研工作室 — 自动刷新行情+信号并推送
#
# 调度时间表（launchd每5分钟触发，脚本内判断是否执行）:
#   06:00       — 盘前信号（美股隔夜+公告评估）
#   09:25       — 集合竞价信号 + 行情刷新
#   09:30-11:30 — 每5分钟刷新行情
#   10:00/10:30/11:00/11:30 — 信号更新（整点和半点）
#   13:00-15:15 — A股行情每5分钟 + 信号每30分钟
#   15:15       — A股收盘最终信号
#   15:16-16:15 — 仅港股行情+信号
#   16:15       — 港股收盘最终信号
#
# 用法: chmod +x auto-refresh.sh && ./auto-refresh.sh

set -e
cd "$(dirname "$0")"

LOCKDIR="/tmp/openclaw-invest-refresh.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    OLD_PID=""
    if [ -f "$LOCKDIR/pid" ]; then
        OLD_PID="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
    fi
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date): 上一轮刷新仍在运行，跳过"
        exit 0
    fi
    rm -rf "$LOCKDIR"
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        echo "$(date): 刷新锁被占用，跳过"
        exit 0
    fi
fi
echo "$$" > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

DOW=$(date +%u)  # 1=Mon, 7=Sun
HOUR=$(date +%H)
MIN=$(date +%M)
HHMM="${HOUR}${MIN}"

if [ "$DOW" -gt 5 ]; then
    echo "$(date): 周末，跳过"
    exit 0
fi

# 绕过代理
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

# MX_APIKEY must be provided by the caller environment or launchd config.
# Never hardcode credentials in this public dashboard repo.
if [ -z "${MX_APIKEY:-}" ]; then
    MX_APIKEY="$(/bin/zsh -lc 'printenv MX_APIKEY' 2>/dev/null || true)"
    export MX_APIKEY
fi

# ========== 06:00 盘前信号（5分钟间隔容差: 0555-0610） ==========
if [ "$HHMM" -ge "0555" ] && [ "$HHMM" -le "0610" ]; then
    # 防重入: 检查今日是否已生成盘前信号
    TODAY=$(date +%Y%m%d)
    LOCK="/tmp/openclaw-premarket-${TODAY}.lock"
    if [ -f "$LOCK" ]; then
        echo "$(date): 盘前信号已生成，跳过"
        exit 0
    fi
    echo "$(date): 盘前信号（美股隔夜+公告）..."
    python3 generate-signals.py
    git add data.json
    git diff --cached --quiet || {
        git commit -m "🌙 06:00 盘前信号"
        git push origin main
    }
    touch "$LOCK"
    echo "$(date): 盘前信号完成"
    exit 0
fi

# ========== 非交易时间 ==========
if [ "$HHMM" -lt "0555" ]; then
    echo "$(date): 非交易时间，跳过"
    exit 0
fi
if [ "$HHMM" -gt "0610" ] && [ "$HHMM" -lt "0925" ]; then
    echo "$(date): 非交易时间，跳过"
    exit 0
fi

# ========== 16:16+ 收盘后 ==========
if [ "$HHMM" -gt "1615" ]; then
    echo "$(date): 收盘后，跳过"
    exit 0
fi

echo "$(date): 开始刷新..."

# --- 判断是否需要生成信号（整点/半点 + 关键时间点）---
RUN_SIGNAL=false
case "$HHMM" in
    0925|1000|1030|1100|1130)
        RUN_SIGNAL=true ;;
    1300|1330|1400|1430|1500|1515|1615)
        RUN_SIGNAL=true ;;
esac

# --- 判断行情刷新范围 ---
SKIP_A=false
SKIP_HK=false

# 15:16-16:15: 只刷港股
if [ "$HHMM" -gt "1515" ] && [ "$HHMM" -le "1615" ]; then
    SKIP_A=true
fi

# 11:31-12:59: 午休，可以跳过（或只刷港股）
if [ "$HHMM" -gt "1130" ] && [ "$HHMM" -lt "1300" ]; then
    SKIP_A=true
    echo "$(date): A股午休，仅刷新港股"
fi

# --- 刷新行情 ---
if [ "$SKIP_A" = true ]; then
    python3 refresh-data.py --skip-a
else
    python3 refresh-data.py
fi

# 刷新自选股行情
python3 manage-watchlist.py refresh

# --- 信号生成 ---
if [ "$RUN_SIGNAL" = true ]; then
    echo "$(date): 生成信号..."
    python3 generate-signals.py
fi

# --- 公开看板防呆：不要把空数据或缺信号快照推线上 ---
python3 - <<'PY'
import json
import sys

with open("data.json", encoding="utf-8") as f:
    data = json.load(f)

layer_stocks = [s for layer in data.get("layers", []) for s in layer.get("stocks", [])]
etfs = data.get("etfs", [])
hk = data.get("hk", [])
if not layer_stocks or not etfs or not hk:
    raise SystemExit("[abort] data.json 市场区块为空，停止推送")

rows = layer_stocks + etfs + hk
missing = [s.get("name") or s.get("code") for s in rows if not s.get("signal")]
if missing:
    raise SystemExit("[abort] data.json 信号字段缺失，停止推送: " + ", ".join(missing[:8]))
PY

# --- 推送 ---
git add data.json watchlist.json
git diff --cached --quiet || {
    MSG="📈 $(date +%H:%M) 行情更新"
    if [ "$RUN_SIGNAL" = true ]; then
        MSG="📊 $(date +%H:%M) 行情+信号更新"
    fi
    git commit -m "$MSG"
    git push origin main
    echo "$(date): 已推送到GitHub"
}

echo "$(date): 完成"
