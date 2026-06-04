#!/usr/bin/env python3
"""Add public market snapshots to strategy.json stock cards.

The script reads only public dashboard data plus MX_APIKEY from the environment.
It never prints or stores credentials. Output fields are public quote/valuation
fields used by the H5 dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STRATEGY_FILE = ROOT / "strategy.json"
REFRESH_DATA = ROOT / "refresh-data.py"
BATCH_SIZE = 8


def _num(value):
    if value in (None, "", "-", "—"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").replace("+", "").strip())
    except Exception:
        return None


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _valuation_adjustment(pe):
    if pe is None:
        return 0, "missing", "估值未核实"
    if pe <= 0:
        return -12, "loss_or_invalid", f"PE {pe:g} 为负/不适用，盈利质量需核实"
    if pe <= 35:
        return 10, "reasonable", f"PE {pe:g} 相对可接受"
    if pe <= 60:
        return 0, "neutral_or_growth_priced", f"PE {pe:g} 已计入成长预期"
    if pe <= 80:
        return -8, "pricey", f"PE {pe:g} 偏高"
    return -18, "expensive", f"PE {pe:g} 显著偏高"


def _split_metric(value) -> tuple[float | None, str]:
    if value in (None, "", "-", "—"):
        return None, ""
    text = str(value).strip()
    if "|" in text:
        raw, period = text.split("|", 1)
    else:
        raw, period = text, ""
    return _num(raw), period.strip()


def _quality_label(score: float) -> str:
    if score >= 75:
        return "高质量成长"
    if score >= 62:
        return "质量良好"
    if score >= 45:
        return "质量待确认"
    return "基本面承压"


def _quality_adjustment(fundamental: dict) -> tuple[int, str]:
    score = _num(fundamental.get("qualityScore"))
    label = fundamental.get("qualityLabel") or "基本面未核实"
    if score is None:
        return -5, label
    if score >= 75:
        return 12, label
    if score >= 62:
        return 7, label
    if score >= 50:
        return 3, label
    if score < 40:
        return -12, label
    return 0, label


def _fundamental_snapshot_from_mx(row: dict, now: str) -> dict:
    def find(*needles):
        for key, value in row.items():
            k = str(key)
            if all(n in k for n in needles):
                return value
        return None

    roe, roe_period = _split_metric(find("净资产收益率"))
    gross, gross_period = _split_metric(find("毛利率"))
    revenue_growth, revenue_period = _split_metric(find("营业收入", "同比"))
    profit_growth, profit_period = _split_metric(find("净利润", "同比"))
    pb, _ = _split_metric(find("市净率"))

    score = 50
    reasons: list[str] = []
    if roe is not None:
        if roe >= 15:
            score += 20
        elif roe >= 10:
            score += 12
        elif roe >= 5:
            score += 5
        elif roe < 3:
            score -= 10
        reasons.append(f"ROE {roe:g}%")
    if gross is not None:
        if gross >= 35:
            score += 12
        elif gross >= 20:
            score += 6
        elif gross < 10:
            score -= 8
        reasons.append(f"毛利率 {gross:g}%")
    if profit_growth is not None:
        if profit_growth >= 30:
            score += 15
        elif profit_growth >= 10:
            score += 8
        elif profit_growth < 0:
            score -= 12
        reasons.append(f"利润增速 {profit_growth:g}%")
    if revenue_growth is not None:
        if revenue_growth >= 30:
            score += 12
        elif revenue_growth >= 10:
            score += 6
        elif revenue_growth < 0:
            score -= 10
        reasons.append(f"营收增速 {revenue_growth:g}%")
    if pb is not None:
        if pb > 15:
            score -= 8
        elif 0 < pb <= 3:
            score += 4
        reasons.append(f"PB {pb:g}")

    score = round(_clamp(score), 1)
    return {
        "roe": roe,
        "roePeriod": roe_period,
        "grossMargin": gross,
        "grossMarginPeriod": gross_period,
        "revenueGrowth": revenue_growth,
        "revenueGrowthPeriod": revenue_period,
        "profitGrowth": profit_growth,
        "profitGrowthPeriod": profit_period,
        "pb": pb,
        "qualityScore": score,
        "qualityLabel": _quality_label(score),
        "reasons": reasons[:5],
        "source": "mx_iwencai",
        "updateTime": now,
    }


def _fetch_fundamentals(mod, a_cards: list[tuple[str, dict]], now: str) -> dict[str, dict]:
    if not os.environ.get("MX_APIKEY"):
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(a_cards), BATCH_SIZE):
        batch = a_cards[i:i + BATCH_SIZE]
        names = [card["name"] for _, card in batch]
        name_set = {card["name"] for _, card in batch}
        code_by_name = {card["name"]: code for code, card in batch}
        code_set = {code for code, _ in batch}
        query = (
            f"{' '.join(names)} 净资产收益率 毛利率 营业收入同比增长 "
            "净利润同比增长 市净率 动态市盈率 总市值"
        )
        print(f"  基本面: {query}")
        rows = mod.parse_mx_response(mod.mx_query(query))
        for row in rows:
            row_code = str(row.get("代码") or "").strip()
            if row_code in code_set:
                out[row_code] = _fundamental_snapshot_from_mx(row, now)
                continue
            name = ""
            for key in ("股票简称", "名称", "股票名称", "简称"):
                if key in row:
                    name = str(row[key]).strip()
                    break
            if not name or name not in name_set:
                continue
            out[code_by_name[name]] = _fundamental_snapshot_from_mx(row, now)
        if i + BATCH_SIZE < len(a_cards):
            time.sleep(0.3)
    print(f"[ok] strategy fundamentals: {len(out)}/{len(a_cards)}")
    return out


def _timing_state(quant: dict) -> tuple[str, str]:
    label = str(quant.get("label") or "")
    signal = str(quant.get("signal") or "")
    score = _num(quant.get("score"))
    rsi = _num(quant.get("rsi"))
    if signal == "risk":
        return "risk", label or "量化风险"
    if signal == "buy":
        return "entry_candidate", label or "买点候选"
    if "强势但等回撤" in label:
        return "wait_pullback", label
    if score is not None and score >= 75 and rsi is not None and rsi >= 75:
        return "wait_pullback", label or "强势等待回撤"
    if score is not None and score >= 70:
        return "trend_watch", label or "趋势观察"
    if label:
        return "wait_confirm", label
    return "wait_confirm", "量化数据待确认"


def _price_level(price, multiplier: float):
    p = _num(price)
    if p is None or p <= 0:
        return None
    return round(p * multiplier, 2)


def _fmt_level(value) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "待行情确认"


def _timing_plan(*, timing_state: str, timing_label: str, action_hint: str,
                 market: dict, quant: dict) -> dict:
    price = _num(market.get("p"))
    score = _num(quant.get("score"))
    momentum = _num(quant.get("momentum"))
    flow = _num(quant.get("flow"))
    rsi = _num(quant.get("rsi"))
    cv = _num(quant.get("cv"))
    source = str(quant.get("source") or "")
    basis = []
    if source == "market_snapshot_fallback":
        basis.append(f"行情择时 {timing_label}")
        if cv is not None:
            basis.append(f"当日涨跌 {cv:g}%")
    elif score is not None:
        basis.append(f"六维 {score:g}")
        if momentum is not None:
            basis.append(f"动量 {momentum:g}")
        if flow is not None:
            basis.append(f"资金 {flow:g}")
        if rsi is not None:
            basis.append(f"RSI {rsi:g}")
    if not basis:
        basis.append("量化快照待补")

    if timing_state == "risk":
        risk_source = "行情" if source == "market_snapshot_fallback" else "六维或动量"
        return {
            "stance": "防守/减仓观察",
            "entry": f"不新增；等待{risk_source}风险解除",
            "invalid": f"{risk_source}仍处风险区",
            "riskControl": "已持有只做人工减仓评估；未持有不介入",
            "takeProfit": "风险状态不设进攻止盈",
            "positionHint": "降仓或空仓观察",
            "basis": basis,
        }

    if timing_state == "wait_pullback":
        low = _fmt_level(_price_level(price, 0.95))
        high = _fmt_level(_price_level(price, 0.97))
        stop = _fmt_level(_price_level(price, 0.92))
        tp = _fmt_level(_price_level(price, 1.08))
        return {
            "stance": "强势等回撤",
            "entry": f"回落至 {low}-{high} 且择时不转弱再评估",
            "invalid": f"跌破 {stop} 或后续择时转 risk",
            "riskControl": f"追高禁入；若已持有，以 {stop} 作为人工防守参考",
            "takeProfit": f"重新放量上攻至 {tp} 附近开始分批止盈评估",
            "positionHint": "小仓观察，不追高",
            "basis": basis,
        }

    if timing_state == "entry_candidate":
        entry = _fmt_level(_price_level(price, 0.99))
        stop = _fmt_level(_price_level(price, 0.94))
        tp = _fmt_level(_price_level(price, 1.10))
        return {
            "stance": "买点候选",
            "entry": f"现价附近或回踩 {entry} 企稳后人工确认",
            "invalid": f"跌破 {stop} 或资金流转弱",
            "riskControl": f"以 {stop} 为初始防守参考",
            "takeProfit": f"上冲至 {tp} 附近观察分批止盈",
            "positionHint": "轻仓试错，确认后再加",
            "basis": basis,
        }

    if timing_state == "trend_watch":
        breakout = _fmt_level(_price_level(price, 1.02))
        pullback = _fmt_level(_price_level(price, 0.98))
        stop = _fmt_level(_price_level(price, 0.94))
        tp = _fmt_level(_price_level(price, 1.10))
        return {
            "stance": "趋势确认",
            "entry": f"突破 {breakout} 或回踩 {pullback} 企稳再评估",
            "invalid": f"跌破 {stop} 或动量低于 45",
            "riskControl": f"以 {stop} 为人工防守参考",
            "takeProfit": f"趋势延续至 {tp} 附近分批兑现评估",
            "positionHint": "等待确认，不提前重仓",
            "basis": basis,
        }

    if source == "market_snapshot_fallback":
        return {
            "stance": "等待六维确认",
            "entry": "行情兜底不生成买点；等待六维评分>=70且动量>=70",
            "invalid": "出现 risk 信号或结构逻辑证伪",
            "riskControl": "没有六维确认前不加仓",
            "takeProfit": "暂无有效进攻计划",
            "positionHint": "观察仓或空仓",
            "basis": basis,
        }

    return {
        "stance": "等待量化确认",
        "entry": "等待六维评分>=70且动量>=70",
        "invalid": "出现 risk 信号或结构逻辑证伪",
        "riskControl": "没有量化确认前不加仓",
        "takeProfit": "暂无有效进攻计划",
        "positionHint": "观察仓或空仓",
        "basis": basis,
    }


def _quote_timing_snapshot(market: dict) -> dict | None:
    """Conservative timing fallback when six-factor data is unavailable.

    This never promotes a buy signal. It only marks defensive or wait states
    from public quote movement so H5 can show an explicit low-confidence plan.
    """
    price = _num(market.get("p"))
    cv = _num(market.get("cv"))
    if cv is None:
        cv = _num(market.get("c"))
    if price is None and cv is None:
        return None

    label = "行情待确认"
    signal = "neutral"
    score = 50
    note = "仅行情择时兜底；未取得六维评分，不生成进攻买点"

    if cv is not None and cv <= -5:
        label = "急跌防守"
        signal = "risk"
        score = 42
        note = f"当日跌幅 {cv:g}%；先做防守观察，等待企稳"
    elif cv is not None and cv <= -3:
        label = "回撤观察"
        score = 48
        note = f"当日回撤 {cv:g}%；不直接视为买点，等待量价企稳"
    elif cv is not None and cv >= 5:
        label = "强势但等回撤"
        score = 47
        note = f"当日涨幅 {cv:g}%；追高风险较高，等待回撤"
    elif cv is not None and cv >= 2:
        label = "短线偏强"
        score = 52
        note = f"当日涨幅 {cv:g}%；仅作趋势观察，等待六维确认"

    return {
        "label": label,
        "signal": signal,
        "note": note,
        "score": score,
        "cv": cv,
        "confidence": "低",
        "source": "market_snapshot_fallback",
        "ts": market.get("updateTime"),
    }


def _decision_profile(card: dict) -> dict:
    selection = card.get("selectionProfile") or {}
    market = card.get("marketSnapshot") or {}
    quant = card.get("quantSnapshot") or card.get("quoteTimingSnapshot") or {}
    fundamental = card.get("fundamentalSnapshot") or {}

    selection_score = _num(selection.get("score"))
    base_value = selection_score if selection_score is not None else 50
    pe = _num(market.get("pe"))
    pe_adj, valuation_state, valuation_note = _valuation_adjustment(pe)
    quality_adj, quality_note = _quality_adjustment(fundamental)
    value_score = _clamp(base_value + pe_adj + quality_adj)
    has_quality = _num(fundamental.get("qualityScore")) is not None

    quant_score = _num(quant.get("score"))
    timing_state, timing_label = _timing_state(quant)
    timing_score = quant_score if quant_score is not None else 50
    decision_score = _clamp(value_score * 0.6 + timing_score * 0.4)
    if valuation_state == "expensive":
        decision_score = min(decision_score, 69)
    elif valuation_state == "pricey":
        decision_score = min(decision_score, 74)
    elif valuation_state == "loss_or_invalid":
        decision_score = min(decision_score, 55)
    elif valuation_state == "missing":
        decision_score = min(decision_score, 65)
    if not has_quality:
        decision_score = min(decision_score, 74)
    decision_score = round(decision_score, 1)
    valuation_ok = valuation_state in ("reasonable", "neutral_or_growth_priced")

    is_quote_timing = quant.get("source") == "market_snapshot_fallback"

    if timing_state == "risk":
        label = "行情风险，暂缓" if is_quote_timing else "量化风险，暂缓"
        action_hint = "暂缓"
    elif value_score >= 75 and valuation_state in ("pricey", "expensive"):
        label = "估值偏贵，等待回撤"
        action_hint = "等回撤"
    elif value_score >= 75 and not has_quality:
        label = "结构候选，基本面待核实"
        action_hint = "先核实基本面"
    elif value_score >= 75 and valuation_ok and timing_state == "entry_candidate":
        label = "价值+量化共振"
        action_hint = "加入观察"
    elif value_score >= 75 and valuation_ok and timing_state in ("trend_watch", "wait_pullback"):
        label = "价值优先，等待买点"
        action_hint = "等回撤" if timing_state == "wait_pullback" else "等买点确认"
    elif valuation_state == "expensive":
        label = "估值偏贵，等待回撤"
        action_hint = "等回撤"
    elif value_score >= 65 and timing_state == "trend_watch":
        label = "结构候选，趋势观察"
        action_hint = "等买点确认"
    else:
        label = "候选观察"
        action_hint = "继续观察"
    plan = _timing_plan(
        timing_state=timing_state,
        timing_label=timing_label,
        action_hint=action_hint,
        market=market,
        quant=quant,
    )

    reasons: list[str] = []
    if selection.get("label"):
        reasons.append(str(selection.get("label")))
    for reason in selection.get("reasons") or []:
        if reason and reason not in reasons:
            reasons.append(str(reason))
        if len(reasons) >= 3:
            break
    reasons.append(valuation_note)
    if fundamental:
        reasons.append(quality_note)
    if quant:
        prefix = "行情择时" if is_quote_timing else "量化"
        reasons.append(f"{timing_label} / {prefix}分 {timing_score:g}")

    return {
        "label": label,
        "score": decision_score,
        "actionHint": action_hint,
        "valueScore": round(value_score, 1),
        "timingScore": round(timing_score, 1),
        "valuationState": valuation_state,
        "qualityState": fundamental.get("qualityLabel") or "基本面未核实",
        "timingState": timing_state,
        "timingPlan": plan,
        "reasons": reasons[:5],
    }


def _attach_decision_profiles(data: dict, now: str) -> None:
    cards = (data.get("stockCards") or {}).get("cards") or {}
    decision_count = 0
    resonance = 0
    value_high = 0
    timing_ready = 0
    timing_plan = 0

    for card in cards.values():
        if not card.get("quantSnapshot") and not card.get("quoteTimingSnapshot"):
            quote_timing = _quote_timing_snapshot(card.get("marketSnapshot") or {})
            if quote_timing:
                card["quoteTimingSnapshot"] = quote_timing
        profile = _decision_profile(card)
        card["decisionProfile"] = profile
        decision_count += 1
        if profile.get("timingPlan"):
            timing_plan += 1
        if profile["valueScore"] >= 75:
            value_high += 1
        if profile["timingState"] in ("entry_candidate", "trend_watch", "wait_pullback"):
            timing_ready += 1
        if profile["label"] == "价值+量化共振":
            resonance += 1

    data.setdefault("stockCards", {})["decisionCoverage"] = {
        "profiled": decision_count,
        "valueHigh": value_high,
        "timingReady": timing_ready,
        "timingPlan": timing_plan,
        "resonance": resonance,
        "total": len(cards),
        "updateTime": now,
    }


def _load_refresh_data():
    if not os.environ.get("MX_APIKEY"):
        try:
            key = subprocess.check_output(
                ["/bin/zsh", "-lc", "printenv MX_APIKEY"],
                text=True,
                timeout=10,
            ).strip()
            if key:
                os.environ["MX_APIKEY"] = key
        except Exception:
            pass
    spec = importlib.util.spec_from_file_location("refresh_data", REFRESH_DATA)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 refresh-data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt_change(value) -> tuple[str, float]:
    try:
        cv = float(str(value).replace("%", "").replace("+", "").strip())
    except Exception:
        cv = 0.0
    return (f"+{cv}%" if cv > 0 else f"{cv}%"), cv


def _snapshot_missing(field: str, value) -> bool:
    if value in (None, "", "-"):
        return True
    if field == "p":
        num = _num(value)
        return num is None or num <= 0
    return False


def _snapshot_from_mx(row: dict) -> dict:
    chg, cv = _fmt_change(row.get("chg"))
    return {
        "p": str(row.get("price") or "-"),
        "c": chg,
        "cv": cv,
        "pe": str(row.get("pe") or "-"),
        "cap": str(row.get("cap") or "-"),
        "source": "mx_iwencai",
    }


def _snapshot_from_sina(row: dict) -> dict:
    return {
        "p": str(row.get("price") or "-"),
        "c": str(row.get("chg") or "0%"),
        "cv": row.get("cv", 0),
        "pe": str(row.get("pe") or "-"),
        "cap": str(row.get("cap") or "-"),
        "source": "sina",
    }


def main() -> int:
    data = json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
    cards = (data.get("stockCards") or {}).get("cards") or {}
    a_cards = [
        (code, card)
        for code, card in cards.items()
        if str(code).isdigit() and len(str(code)) == 6 and card.get("name")
    ]
    if not a_cards:
        print("[warn] strategy.json 没有可增强的 A 股候选")
        return 0

    mod = _load_refresh_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fundamentals = _fetch_fundamentals(mod, a_cards, now)
    mx_by_name: dict[str, dict] = {}
    if os.environ.get("MX_APIKEY"):
        for i in range(0, len(a_cards), BATCH_SIZE):
            batch = a_cards[i:i + BATCH_SIZE]
            names = [card["name"] for _, card in batch]
            codes = [code for code, _ in batch]
            mx_by_name.update(mod.fetch_a_stock_quotes(codes, names))
            if i + BATCH_SIZE < len(a_cards):
                time.sleep(0.4)
    else:
        print("[warn] MX_APIKEY 未配置，跳过 PE/市值增强")

    codes = [code for code, _ in a_cards]
    sina = mod.fetch_sina_quotes(codes)
    tencent = mod.fetch_tencent_quotes(codes)
    ak_quotes = mod.fetch_akshare_quotes(codes, [])
    enhanced = 0
    pe_count = 0

    for code, card in a_cards:
        snap = None
        mx = mx_by_name.get(card.get("name", ""))
        if mx:
            snap = _snapshot_from_mx(mx)
        if (not snap or snap.get("p") in ("", "-")) and code in sina:
            sina_snap = _snapshot_from_sina(sina[code])
            snap = {**sina_snap, **{k: v for k, v in (snap or {}).items() if v not in ("", "-")}}
            snap.setdefault("source", "sina")
        tc = tencent.get(code)
        if tc:
            tc_snap = _snapshot_from_sina(tc)
            if not snap:
                snap = tc_snap
                snap["source"] = "tencent"
            else:
                for field in ("p", "c", "cv", "pe", "cap"):
                    if _snapshot_missing(field, snap.get(field)) and not _snapshot_missing(field, tc_snap.get(field)):
                        snap[field] = tc_snap[field]
                if snap.get("source") != "mx_iwencai" and tc_snap.get("pe") not in (None, "", "-"):
                    snap["source"] = f"{snap.get('source') or 'sina'}+tencent"
        ak = ak_quotes.get(code)
        if ak:
            ak_snap = _snapshot_from_sina(ak)
            if not snap:
                snap = ak_snap
                snap["source"] = "akshare"
            else:
                for field in ("p", "c", "cv", "pe", "cap"):
                    if _snapshot_missing(field, snap.get(field)) and not _snapshot_missing(field, ak_snap.get(field)):
                        snap[field] = ak_snap[field]
                if snap.get("source") != "mx_iwencai" and ak_snap.get("pe") not in (None, "", "-"):
                    snap["source"] = f"{snap.get('source') or 'sina'}+akshare"
        if not snap:
            continue
        old_snap = card.get("marketSnapshot") or {}
        for sticky_field in ("pe", "cap"):
            if snap.get(sticky_field) in (None, "", "-") and old_snap.get(sticky_field) not in (None, "", "-"):
                snap[sticky_field] = old_snap[sticky_field]
                snap[f"{sticky_field}Source"] = old_snap.get("source") or "previous_verified"
        snap["updateTime"] = now
        card["marketSnapshot"] = snap
        if code in fundamentals:
            card["fundamentalSnapshot"] = fundamentals[code]
        enhanced += 1
        if snap.get("pe") and snap.get("pe") != "-":
            pe_count += 1
    for code, fundamental in fundamentals.items():
        if code in cards:
            cards[code]["fundamentalSnapshot"] = fundamental

    final_market = sum(1 for card in cards.values() if card.get("marketSnapshot"))
    final_pe = sum(
        1 for card in cards.values()
        if (card.get("marketSnapshot") or {}).get("pe")
        and (card.get("marketSnapshot") or {}).get("pe") != "-"
    )
    final_fundamental = sum(1 for card in cards.values() if card.get("fundamentalSnapshot"))
    data.setdefault("stockCards", {})["marketCoverage"] = {
        "enhanced": final_market,
        "pe": final_pe,
        "fundamental": final_fundamental,
        "total": len(a_cards),
        "updateTime": now,
        "sources": ["mx_iwencai", "sina", "tencent", "akshare"],
    }
    _attach_decision_profiles(data, now)
    STRATEGY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] strategy market snapshots: {enhanced}/{len(a_cards)}, PE {pe_count}; decision profiles {len(cards)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
