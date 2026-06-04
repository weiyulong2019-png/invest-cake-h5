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
    if pe is None or pe <= 0:
        return 0, "missing", "估值未核实"
    if pe <= 35:
        return 10, "reasonable", f"PE {pe:g} 相对可接受"
    if pe <= 60:
        return 0, "neutral_or_growth_priced", f"PE {pe:g} 已计入成长预期"
    if pe <= 80:
        return -8, "pricey", f"PE {pe:g} 偏高"
    return -18, "expensive", f"PE {pe:g} 显著偏高"


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


def _decision_profile(card: dict) -> dict:
    selection = card.get("selectionProfile") or {}
    market = card.get("marketSnapshot") or {}
    quant = card.get("quantSnapshot") or {}

    selection_score = _num(selection.get("score"))
    base_value = selection_score if selection_score is not None else 50
    pe = _num(market.get("pe"))
    pe_adj, valuation_state, valuation_note = _valuation_adjustment(pe)
    value_score = _clamp(base_value + pe_adj)

    quant_score = _num(quant.get("score"))
    timing_state, timing_label = _timing_state(quant)
    timing_score = quant_score if quant_score is not None else 50
    decision_score = round(_clamp(value_score * 0.6 + timing_score * 0.4), 1)

    if timing_state == "risk":
        label = "量化风险，暂缓"
        action_hint = "暂缓"
    elif value_score >= 75 and timing_state == "entry_candidate":
        label = "价值+量化共振"
        action_hint = "加入观察"
    elif value_score >= 75 and timing_state in ("trend_watch", "wait_pullback"):
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

    reasons: list[str] = []
    if selection.get("label"):
        reasons.append(str(selection.get("label")))
    for reason in selection.get("reasons") or []:
        if reason and reason not in reasons:
            reasons.append(str(reason))
        if len(reasons) >= 3:
            break
    reasons.append(valuation_note)
    if quant:
        reasons.append(f"{timing_label} / 量化分 {timing_score:g}")

    return {
        "label": label,
        "score": decision_score,
        "actionHint": action_hint,
        "valueScore": round(value_score, 1),
        "timingScore": round(timing_score, 1),
        "valuationState": valuation_state,
        "timingState": timing_state,
        "reasons": reasons[:5],
    }


def _attach_decision_profiles(data: dict, now: str) -> None:
    cards = (data.get("stockCards") or {}).get("cards") or {}
    decision_count = 0
    resonance = 0
    value_high = 0
    timing_ready = 0

    for card in cards.values():
        profile = _decision_profile(card)
        card["decisionProfile"] = profile
        decision_count += 1
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

    sina = mod.fetch_sina_quotes([code for code, _ in a_cards])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        if not snap:
            continue
        old_snap = card.get("marketSnapshot") or {}
        for sticky_field in ("pe", "cap"):
            if snap.get(sticky_field) in (None, "", "-") and old_snap.get(sticky_field) not in (None, "", "-"):
                snap[sticky_field] = old_snap[sticky_field]
                snap[f"{sticky_field}Source"] = old_snap.get("source") or "previous_verified"
        snap["updateTime"] = now
        card["marketSnapshot"] = snap
        enhanced += 1
        if snap.get("pe") and snap.get("pe") != "-":
            pe_count += 1

    data.setdefault("stockCards", {})["marketCoverage"] = {
        "enhanced": enhanced,
        "pe": pe_count,
        "total": len(a_cards),
        "updateTime": now,
        "sources": ["mx_iwencai", "sina"],
    }
    _attach_decision_profiles(data, now)
    STRATEGY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] strategy market snapshots: {enhanced}/{len(a_cards)}, PE {pe_count}; decision profiles {len(cards)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
