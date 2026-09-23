#!/usr/bin/env python3
"""
信号回测器 — 验证乘法因子模型的真实有效性
==========================================
为什么需要它：
  judge_signal_v2 的 10 个因子系数（1.8/0.3/1.5/0.3…）全部是人工设定的，
  从未用历史数据验证过。本脚本用扶摇历史 K 线滚动重算每一个交易日的指标，
  还原当时的信号，再统计 T+N 的真实收益 → 得出胜率/盈亏比/超额收益。

  没有它，"策略"只是看起来合理；有了它，才知道哪些因子是信号、哪些是噪音。

用法：
  python backtest-signals.py                      # 默认：data.json 全部 A 股，T+5
  python backtest-signals.py --codes 600487,300390
  python backtest-signals.py --days 400 --horizon 10
  python backtest-signals.py --adx-split           # 按 ADX 状态分层看表现

输出：控制台报告 + backtest_result.json
"""

import os
import sys
import json
import math
import argparse
from pathlib import Path
from datetime import datetime
from importlib.util import spec_from_file_location, module_from_spec

# 绕过代理（与 generate-signals.py 一致：行情源直连）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

HERE = Path(__file__).parent


def _load(name: str, path: Path):
    """按文件路径加载模块（支持连字符文件名）"""
    spec = spec_from_file_location(name, path)
    m = module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# 复用 generate-signals.py 的指标函数与打分逻辑（保证回测与实盘口径一致）
gs = _load("gs_signals", HERE / "generate-signals.py")
try:
    import fuyao_source as fuyao
except Exception:
    fuyao = None

WARMUP = 60          # MA60 需要至少 60 根 K 线预热
BENCHMARK = "000300"  # 沪深300，用于算超额收益


# ─────────────────────────── 数据获取 ───────────────────────────

def fetch_bars(code, days):
    """扶摇历史 K 线 → dict(close/high/low/volume/amount/date)，失败返回 None"""
    if fuyao is None or not fuyao.available():
        return None
    try:
        b = fuyao.fetch_history(code, days=days)
    except Exception as e:
        print(f"  [WARN] {code} 取数失败: {e}")
        return None
    if not b or len(b.get("close", [])) < WARMUP + 10:
        return None
    return b


def benchmark_returns(days):
    """基准（沪深300）→ {date_ms: close} 映射，用于按日期对齐算超额。
    指数必须走 index 接口且显式 .SH —— 不能复用个股的 to_thscode。
    按日期而非位置对齐：个股停牌会导致交易日错位。"""
    try:
        b = fuyao.fetch_index_history(BENCHMARK, days=days)
    except Exception as e:
        print(f"  [WARN] 基准取数失败: {e}")
        return None
    if not b or len(b.get("close", [])) < 10:
        return None
    return dict(zip(b.get("date", []), b["close"]))


# ─────────────────────────── 回测核心 ───────────────────────────

def slice_indicators(bars, t):
    """截取 [0, t] 的窗口，算出 t 时刻的指标（与实盘 _fill_indicators 同口径）"""
    closes = bars["close"][:t + 1]
    highs = bars["high"][:t + 1]
    lows = bars["low"][:t + 1]
    volumes = bars.get("volume", [])[:t + 1]
    amounts = bars.get("amount", [])[:t + 1]
    ind = {"code": bars.get("_code", ""), "ok": False}
    try:
        gs._fill_indicators(ind, closes, highs, lows, volumes, amounts, len(closes))
    except Exception:
        return None
    return ind


def rejudge(factors, adx, only=None, flip=None):
    """按 --only/--flip 重新打分，绕开 judge_signal_v2 的硬编码系数。
    only: 只保留这些因子；flip: 这些因子方向取镜像(2-f)。
    返回 (signal, score)"""
    fs = dict(factors or {})
    if only:
        fs = {k: v for k, v in fs.items() if k in only}
    if flip:
        fs = {k: (round(2.0 - v, 3) if k in flip else v) for k, v in fs.items()}
    if not fs:
        return "neutral", 1.0
    base = 1.0
    for f in fs.values():
        base *= f
    if adx is not None:
        if adx < 22 and (base > 1.3 or base < 0.7):
            base = 1.0
        elif adx > 30:
            base = base * 1.1 if base > 1.0 else base * 0.9
    score = round(base, 3)
    if score >= 1.3:
        return "buy", score
    if score <= 0.7:
        return "risk", score
    return "neutral", score


def backtest_one(code, bars, horizon, bench_map=None, only=None, flip=None):
    """对单只股票滚动回测，返回信号列表
    bench_map: {date_ms: close} 基准映射，按日期对齐算超额
    only/flip: 因子子集 / 方向镜像，用于数据驱动重标定"""
    closes = bars["close"]
    n = len(closes)
    out = []
    bench_map = bench_map or {}
    dates = bars.get("date") or [None] * n

    for t in range(WARMUP, n - horizon):
        ind = slice_indicators(bars, t)
        if not ind or not ind.get("ok"):
            continue
        # 回测只用技术面核心因子：美股隔夜/北向/解禁/龙虎榜为实时数据，历史无法复现 → 置中性
        sig, note, conf, factors = gs.judge_signal_v2(
            ind, us_data=None, is_premarket=False, enhancement=None
        )
        if sig == "neutral":
            continue

        # 用 only/flip 重算信号（默认 only/flip 为空 → 与原模型一致）
        sig, score = rejudge(factors, ind.get("adx"), only, flip)

        p0 = closes[t]
        p1 = closes[t + horizon]
        if not p0 or not p1:
            continue
        ret = (p1 - p0) / p0 * 100

        # 超额收益（同期基准，按日期对齐）
        excess = None
        d0, d1 = dates[t], dates[t + horizon]
        b0 = bench_map.get(d0) if d0 is not None else None
        b1 = bench_map.get(d1) if d1 is not None else None
        if b0 and b1 and b0 > 0:
            excess = ret - (b1 - b0) / b0 * 100

        out.append({
            "code": code,
            "date": dates[t],
            "signal": sig,
            "score": score,
            "confidence": conf,
            "price": round(p0, 3),
            f"ret_{horizon}d": round(ret, 2),
            f"excess_{horizon}d": round(excess, 2) if excess is not None else None,
            "adx": ind.get("adx"),
            "rsi": ind.get("rsi"),
            "factors": factors,
        })
    return out


# ─────────────────────────── 统计 ───────────────────────────

def summarize(rows, horizon, label=""):
    """算胜率/盈亏比/平均收益/超额"""
    key = f"ret_{horizon}d"
    ekey = f"excess_{horizon}d"
    if not rows:
        return {"label": label, "n": 0, "note": "无样本"}

    rets = [r[key] for r in rows if r.get(key) is not None]
    if not rets:
        return {"label": label, "n": 0, "note": "无收益数据"}

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / len(rets)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    # 盈亏比：平均盈利 / 平均亏损（>1 才值得跟）
    pl_ratio = (avg_win / avg_loss) if avg_loss > 0 else None

    exc = [r[ekey] for r in rows if r.get(ekey) is not None]
    avg_excess = sum(exc) / len(exc) if exc else None
    win_excess = (sum(1 for e in exc if e > 0) / len(exc)) if exc else None

    # 期望值：胜率×平均盈利 - 败率×平均亏损
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

    return {
        "label": label,
        "n": len(rets),
        "win_rate": round(win_rate, 3),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pl_ratio": round(pl_ratio, 2) if pl_ratio else None,
        "expectancy": round(expectancy, 3),
        "avg_excess": round(avg_excess, 2) if avg_excess is not None else None,
        "win_rate_excess": round(win_excess, 3) if win_excess is not None else None,
    }


def by_adx_state(rows, horizon):
    """按 ADX 分层：<22 震荡 / 22-30 弱趋势 / >30 强趋势"""
    buckets = {"震荡(ADX<22)": [], "弱趋势(22-30)": [], "强趋势(ADX>30)": [], "未知": []}
    for r in rows:
        a = r.get("adx")
        if a is None:
            buckets["未知"].append(r)
        elif a < 22:
            buckets["震荡(ADX<22)"].append(r)
        elif a <= 30:
            buckets["弱趋势(22-30)"].append(r)
        else:
            buckets["强趋势(ADX>30)"].append(r)
    return [summarize(v, horizon, k) for k, v in buckets.items() if v]


def factor_contrib(rows, horizon):
    """单因子有效性：该因子取极端值时，后续收益是否显著不同"""
    key = f"ret_{horizon}d"
    names = ["ma", "ma60", "rsi", "macd", "vol", "momentum"]
    out = []
    for f in names:
        bull = [r[key] for r in rows if (r.get("factors") or {}).get(f, 1.0) > 1.0 and r.get(key) is not None]
        bear = [r[key] for r in rows if (r.get("factors") or {}).get(f, 1.0) < 1.0 and r.get(key) is not None]
        if len(bull) < 5 or len(bear) < 5:
            out.append({"factor": f, "note": "样本不足"})
            continue
        mb, mbe = sum(bull) / len(bull), sum(bear) / len(bear)
        out.append({
            "factor": f,
            "n_bull": len(bull), "avg_bull": round(mb, 2),
            "n_bear": len(bear), "avg_bear": round(mbe, 2),
            "spread": round(mb - mbe, 2),   # >0 说明该因子有正向区分度
        })
    return out


# ─────────────────────────── 主流程 ───────────────────────────

def load_codes(args):
    if args.codes:
        return [c.strip() for c in args.codes.split(",") if c.strip()]
    data_file = HERE / "data.json"
    if not data_file.exists():
        return []
    d = json.loads(data_file.read_text(encoding="utf-8"))
    codes, seen = [], set()
    for layer in d.get("layers", []):
        for s in layer.get("stocks", []):
            c = s.get("code", "")
            # 只回测 A 股（扶摇历史不支持港股；ETF 走基金接口另议）
            if c and c not in seen and not c.startswith("HK.") and len(c) == 6:
                seen.add(c)
                codes.append(c)
    return codes


def main():
    ap = argparse.ArgumentParser(description="信号回测：验证乘法因子模型有效性")
    ap.add_argument("--codes", default="", help="指定代码，逗号分隔（默认 data.json 全部 A 股）")
    ap.add_argument("--days", type=int, default=400, help="历史长度（默认 400）")
    ap.add_argument("--horizon", type=int, default=5, help="持有周期 T+N（默认 5）")
    ap.add_argument("--adx-split", action="store_true", help="按 ADX 状态分层输出")
    ap.add_argument("--only", default="", help="只启用这些因子（逗号分隔），用于验证剔除无效因子")
    ap.add_argument("--flip", default="", help="这些因子方向取镜像(2-f)，用于验证方向是否设反")
    ap.add_argument("--out", default="backtest_result.json", help="结果输出文件")
    args = ap.parse_args()

    only = {c.strip() for c in args.only.split(",") if c.strip()} or None
    flip = {c.strip() for c in args.flip.split(",") if c.strip()} or None

    if fuyao is None or not fuyao.available():
        print("[ERROR] 扶摇不可用（检查 .env.local 的 FUYAO_API_KEY）")
        return 1

    codes = load_codes(args)
    if not codes:
        print("[ERROR] 无标的可回测")
        return 1

    print("=== 信号回测：乘法因子模型验证 ===")
    print(f"标的 {len(codes)} 只 · 历史 {args.days} 天 · 持有 T+{args.horizon}")
    print(f"基准 {BENCHMARK}（沪深300）· 预热 {WARMUP} 根")
    print("注：回测仅复现阶段性技术因子（美股隔夜/北向/解禁/龙虎榜为实时数据，置中性）")
    print()

    bench = benchmark_returns(args.days)
    if bench:
        print(f"[基准] 沪深300 取到 {len(bench)} 个交易日（按日期对齐）")
    else:
        print("[基准] 沪深300 不可用 → 只统计绝对收益，不算超额")
    print()

    all_rows = []
    for i, code in enumerate(codes, 1):
        bars = fetch_bars(code, args.days)
        if not bars:
            print(f"  [{i}/{len(codes)}] {code} 跳过（数据不足）")
            continue
        bars["_code"] = code
        rows = backtest_one(code, bars, args.horizon, bench, only, flip)
        all_rows.extend(rows)
        print(f"  [{i}/{len(codes)}] {code} 信号 {len(rows)} 条")

    if not all_rows:
        print("\n[ERROR] 未产生任何信号样本")
        return 1

    buys = [r for r in all_rows if r["signal"] == "buy"]
    risks = [r for r in all_rows if r["signal"] == "risk"]

    print()
    print("=" * 62)
    print(f"总信号 {len(all_rows)} 条 · buy {len(buys)} · risk {len(risks)}")
    print("=" * 62)

    print("\n【buy 信号（做多）】")
    s = summarize(buys, args.horizon, "buy")
    for k, v in s.items():
        print(f"  {k:16} {v}")

    print("\n【risk 信号（规避/做空）】— 收益为负=判断正确")
    s2 = summarize(risks, args.horizon, "risk")
    for k, v in s2.items():
        print(f"  {k:16} {v}")

    print("\n【单因子区分度】spread = 看多组均值 - 看空组均值，>0 才说明因子有效")
    for f in factor_contrib(all_rows, args.horizon):
        print(f"  {f}")

    if args.adx_split:
        print("\n【按 ADX 分层（buy 信号）】")
        for r in by_adx_state(buys, args.horizon):
            print(f"  {r['label']:16} n={r.get('n')} win={r.get('win_rate')} avg={r.get('avg_ret')} pl={r.get('pl_ratio')}")

    # 落盘
    out_path = HERE / args.out
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"days": args.days, "horizon": args.horizon, "codes": codes,
                   "benchmark": BENCHMARK, "warmup": WARMUP,
                   "only": sorted(only) if only else None,
                   "flip": sorted(flip) if flip else None},
        "buy": summarize(buys, args.horizon, "buy"),
        "risk": summarize(risks, args.horizon, "risk"),
        "factors": factor_contrib(all_rows, args.horizon),
        "adx_split_buy": by_adx_state(buys, args.horizon) if args.adx_split else None,
        "samples": all_rows[:500],  # 只留部分样本，避免文件过大
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out_path}")

    # 结论判读（第一性原理：正期望才值得跟）
    print("\n" + "=" * 62)
    b = payload["buy"]
    if b.get("n", 0) < 30:
        print("⚠️ 样本不足 30，统计不显著，需扩大标的或拉长历史")
    else:
        exp = b.get("expectancy", 0)
        pl = b.get("pl_ratio")
        if exp > 0 and (pl or 0) > 1:
            print(f"✅ buy 信号正期望：每笔期望 {exp}% · 盈亏比 {pl}")
        elif exp > 0:
            print(f"⚠️ buy 期望为正({exp}%)但盈亏比 {pl} ≤1：靠高胜率硬撑，抗风险差")
        else:
            print(f"❌ buy 信号负期望({exp}%)：该模型在当前参数下不可用，需调参或重构")
    return 0


if __name__ == "__main__":
    sys.exit(main())
