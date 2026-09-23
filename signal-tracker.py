#!/usr/bin/env python3
"""
信号追踪器 — 实盘复盘闭环
==========================
回测告诉我们"历史上这套参数行不行"，追踪器告诉我们"现在的信号到底准不准"。
两者缺一不可：回测会被市场风格切换打脸，追踪器是唯一的实时纠错机制。

功能：
  record  把当次生成的信号入库（每日自动跑，幂等：同一 code+signal_time 只记一次）
  settle  到期结算：用扶摇取当前价，算 T+1/T+5/T+20 收益与超额（基准沪深300）
  stats   按 agent 聚合：胜率 / 盈亏比 / 期望 / 超额胜率

用法：
  python signal-tracker.py record            # 从 data.json 记录当前信号
  python signal-tracker.py settle            # 结算所有到期未结信号
  python signal-tracker.py stats             # 查看统计
  python signal-tracker.py stats --min 20    # 只看样本≥20 的

存储：SQLite，默认 .tracker/signals.db（已在 .gitignore，不入库、不外泄）
"""

import os
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timedelta

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

HERE = Path(__file__).parent
DB_PATH = HERE / ".tracker" / "signals.db"
DEFAULT_AGENT = "technical_v2"      # 当前引擎：乘法因子模型 v2
BENCHMARK = "000300"                # 沪深300
HORIZONS = (1, 5, 20)               # 跟踪周期（交易日）


def _load(name, path):
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(name, path)
    m = module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


try:
    import fuyao_source as fuyao
except Exception:
    fuyao = None


# ─────────────────────────── DB ───────────────────────────

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        code          TEXT NOT NULL,
        name          TEXT,
        agent         TEXT NOT NULL DEFAULT 'technical_v2',
        signal        TEXT NOT NULL,          -- buy / risk / neutral
        confidence    TEXT,                   -- 高 / 中 / 低
        signal_time   TEXT NOT NULL,          -- 信号产生时间 YYYY-MM-DD HH:MM
        price         REAL,                   -- 信号时价格
        note          TEXT,
        score         REAL,
        settled       INTEGER DEFAULT 0,      -- 是否已结算
        created_at    TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_signal
        ON signals(code, agent, signal_time);

    CREATE TABLE IF NOT EXISTS outcomes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER NOT NULL,
        horizon     INTEGER NOT NULL,         -- 1 / 5 / 20
        ret         REAL,                     -- 绝对收益 %
        excess      REAL,                     -- 超额收益 %（减沪深300同期）
        settled_at  TEXT,
        UNIQUE(signal_id, horizon)
    );
    """)
    conn.commit()


# ─────────────────────────── record ───────────────────────────

def cmd_record(args):
    """从 data.json 读取当前信号入库（幂等）"""
    data_file = HERE / "data.json"
    if not data_file.exists():
        print("[ERROR] data.json 不存在，先跑 refresh-data.py + generate-signals.py")
        return 1
    d = json.loads(data_file.read_text(encoding="utf-8"))
    ts = d.get("updateTime") or datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = connect()
    init_db(conn)
    n_new = n_skip = 0
    for layer in d.get("layers", []):
        for s in layer.get("stocks", []):
            sig = s.get("signal")
            if sig not in ("buy", "risk"):
                continue
            code = s.get("code")
            if not code:
                continue
            price = s.get("p")
            try:
                price = float(str(price).replace(",", ""))
            except (TypeError, ValueError):
                price = None
            if not price:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO signals "
                    "(code,name,agent,signal,confidence,signal_time,price,note,score,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (code, s.get("name"), DEFAULT_AGENT, sig, s.get("confidence"),
                     ts, price, s.get("signalNote"), s.get("score"),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                if conn.total_changes and conn.execute(
                        "SELECT changes()").fetchone()[0]:
                    n_new += 1
                else:
                    n_skip += 1
            except Exception as e:
                print(f"  [WARN] {code} 入库失败: {e}")
                n_skip += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE settled=0").fetchone()[0]
    conn.close()
    print(f"[record] 新增 {n_new} · 跳过(已存在) {n_skip}")
    print(f"         库内累计 {total} 条信号 · 待结算 {pending} 条")
    print(f"         数据库 {DB_PATH}")
    return 0


# ─────────────────────────── settle ───────────────────────────

def _price_on(bars, date_key):
    """按日期取收盘价（bars: fetch_history 结果）"""
    if not bars:
        return None
    dates = bars.get("date") or []
    closes = bars.get("close") or []
    m = dict(zip(dates, closes))
    return m.get(date_key)


def cmd_settle(args):
    """结算到期信号：算 T+1/T+5/T+20 收益与超额"""
    if fuyao is None or not fuyao.available():
        print("[ERROR] 扶摇不可用，无法取价结算")
        return 1

    conn = connect()
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM signals WHERE settled=0 ORDER BY signal_time").fetchall()
    if not rows:
        print("[settle] 无待结算信号")
        conn.close()
        return 0

    # 基准序列（按日期对齐）
    bench = {}
    try:
        b = fuyao.fetch_index_history(BENCHMARK, days=60)
        bench = dict(zip(b.get("date", []), b.get("close", [])))
    except Exception as e:
        print(f"  [WARN] 基准不可用，只算绝对收益: {e}")

    # 按 code 批量取历史，避免重复请求
    cache = {}
    done = skipped = 0
    for r in rows:
        code = r["code"]
        if code.startswith("HK."):
            continue
        if code not in cache:
            try:
                cache[code] = fuyao.fetch_history(code, days=60)
            except Exception:
                cache[code] = None
        bars = cache.get(code)
        if not bars or not bars.get("date"):
            skipped += 1
            continue

        dates = bars["date"]
        closes = bars["close"]
        # 找信号日在该股票序列中的位置（按日期字符串前缀匹配 YYYY-MM-DD）
        sig_day = r["signal_time"][:10]
        idx = None
        for i, dt in enumerate(dates):
            ds = str(dt)
            # date 可能是 ms 时间戳或日期串，统一比对
            if ds.startswith(sig_day) or (
                ds.isdigit() and len(ds) == 13 and
                datetime.fromtimestamp(int(ds) / 1000).strftime("%Y-%m-%d") == sig_day
            ):
                idx = i
                break
        if idx is None:
            skipped += 1
            continue

        p0 = closes[idx]
        if not p0:
            skipped += 1
            continue

        any_done = False
        for h in HORIZONS:
            j = idx + h
            if j >= len(closes):
                continue
            p1 = closes[j]
            if not p1:
                continue
            ret = (p1 - p0) / p0 * 100
            excess = None
            d0, d1 = dates[idx], dates[j]
            b0, b1 = bench.get(d0), bench.get(d1)
            if b0 and b1 and b0 > 0:
                excess = ret - (b1 - b0) / b0 * 100
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO outcomes (signal_id,horizon,ret,excess,settled_at) "
                    "VALUES (?,?,?,?,?)",
                    (r["id"], h, round(ret, 2),
                     round(excess, 2) if excess is not None else None,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                any_done = True
            except Exception:
                pass
        if any_done:
            conn.execute("UPDATE signals SET settled=1 WHERE id=?", (r["id"],))
            done += 1
    conn.commit()
    conn.close()
    print(f"[settle] 结算 {done} 条 · 跳过(数据不足/未到期) {skipped} 条")
    return 0


# ─────────────────────────── stats ───────────────────────────

def _agg(rows):
    if not rows:
        return None
    n = len(rows)
    wins = [r for r in rows if r > 0]
    losses = [r for r in rows if r <= 0]
    wr = len(wins) / n
    aw = sum(wins) / len(wins) if wins else 0.0
    al = abs(sum(losses) / len(losses)) if losses else 0.0
    return {
        "n": n,
        "win_rate": round(wr, 3),
        "avg_ret": round(sum(rows) / n, 2),
        "avg_win": round(aw, 2),
        "avg_loss": round(al, 2),
        "pl_ratio": round(aw / al, 2) if al > 0 else None,
        "expectancy": round(wr * aw - (1 - wr) * al, 2),
    }


def cmd_stats(args):
    conn = connect()
    init_db(conn)
    rows = conn.execute(
        "SELECT s.*, o.horizon, o.ret, o.excess FROM signals s "
        "JOIN outcomes o ON o.signal_id=s.id").fetchall()
    if not rows:
        print("[stats] 尚无已结算信号。先跑 record，等 T+1 后再 settle。")
        conn.close()
        return 0

    print("=== 信号实盘统计 ===")
    print(f"数据库 {DB_PATH}")
    print()
    for sig_type in ("buy", "risk"):
        print(f"【{sig_type}】")
        for h in HORIZONS:
            rets = [r["ret"] for r in rows
                    if r["signal"] == sig_type and r["horizon"] == h and r["ret"] is not None]
            exc = [r["excess"] for r in rows
                   if r["signal"] == sig_type and r["horizon"] == h and r["excess"] is not None]
            a = _agg(rets)
            if not a or a["n"] < args.min:
                print(f"  T+{h:<3} 样本不足（{len(rets)}/{args.min}）")
                continue
            ea = _agg(exc)
            print(f"  T+{h:<3} n={a['n']:<4} 胜率={a['win_rate']:<6} 均值={a['avg_ret']:<7}% "
                  f"盈亏比={a['pl_ratio']} 期望={a['expectancy']}% "
                  f"超额={'%.2f' % ea['avg_ret'] + '%' if ea else '—'}")
        print()

    # 关键诊断：buy vs risk 是否有区分度
    print("【区分度诊断】buy 与 risk 的后续收益是否有差异")
    for h in HORIZONS:
        b = [r["ret"] for r in rows if r["signal"] == "buy" and r["horizon"] == h and r["ret"] is not None]
        rk = [r["ret"] for r in rows if r["signal"] == "risk" and r["horizon"] == h and r["ret"] is not None]
        if len(b) < args.min or len(rk) < args.min:
            print(f"  T+{h:<3} 样本不足")
            continue
        mb, mr = sum(b) / len(b), sum(rk) / len(rk)
        gap = mb - mr
        verdict = "✅ 有区分度" if abs(gap) > 1.0 else "❌ 无区分度（信号无效）"
        print(f"  T+{h:<3} buy均值={mb:.2f}% risk均值={mr:.2f}% 差={gap:.2f}%  {verdict}")
    print()
    print("判读：buy 应显著高于 risk；若两者接近，说明模型只是被大盘带着走，无选股能力。")
    conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="信号追踪：实盘复盘闭环")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("record", help="记录当前信号入库")
    sub.add_parser("settle", help="结算到期信号")
    p_stats = sub.add_parser("stats", help="查看统计")
    p_stats.add_argument("--min", type=int, default=10, help="最小样本量（默认 10）")
    args = ap.parse_args()

    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "settle":
        return cmd_settle(args)
    return cmd_stats(args)


if __name__ == "__main__":
    sys.exit(main())
