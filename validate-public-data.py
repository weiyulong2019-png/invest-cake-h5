#!/usr/bin/env python3
"""Validate public H5 data before deployment.

This guard intentionally checks only public, non-sensitive files. It never reads
portfolio quantities, costs, credentials, or private agent state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXPECTED_CHAINS = {
    "ai_server",
    "new_energy",
    "commercial_space",
    "physical_ai",
    "innovative_drug",
}


def _load(name: str) -> dict:
    path = ROOT / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"[abort] {name} 不存在")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[abort] {name} 不是合法 JSON: {exc}")


def validate_strategy() -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    data = _load("strategy.json")

    stock_cards = data.get("stockCards") or {}
    cards = stock_cards.get("cards") or {}
    chains = data.get("chains") or {}

    if data.get("mode") != "live":
        errors.append("strategy.json mode 不是 live")
    if stock_cards.get("count", 0) <= 0 or not cards:
        errors.append("stockCards 为空")
    if not data.get("chainTree"):
        errors.append("chainTree 为空")

    missing_chains = sorted(EXPECTED_CHAINS - set(chains))
    if missing_chains:
        errors.append("缺少产业链: " + ", ".join(missing_chains))

    empty_chains = [
        key for key, chain in chains.items()
        if not (chain.get("segments") or [])
    ]
    if empty_chains:
        errors.append("产业链 segments 为空: " + ", ".join(empty_chains))

    missing_selection = [
        code for code, card in cards.items()
        if not card.get("selectionProfile")
    ]
    missing_timing = [
        code for code, card in cards.items()
        if not card.get("timingProfile")
    ]
    if missing_selection:
        errors.append(f"selectionProfile 缺失 {len(missing_selection)} 只")
    if missing_timing:
        errors.append(f"timingProfile 缺失 {len(missing_timing)} 只")

    value_candidates = [
        code for code, card in cards.items()
        if (card.get("selectionProfile") or {}).get("label") == "价值优先候选"
    ]
    if not value_candidates:
        warnings.append("没有价值优先候选")
    market_covered = [
        code for code, card in cards.items()
        if card.get("marketSnapshot")
    ]
    pe_covered = [
        code for code, card in cards.items()
        if (card.get("marketSnapshot") or {}).get("pe")
        and (card.get("marketSnapshot") or {}).get("pe") != "-"
    ]
    quant_covered = [
        code for code, card in cards.items()
        if card.get("quantSnapshot")
    ]
    decision_covered = [
        code for code, card in cards.items()
        if card.get("decisionProfile")
    ]
    min_market = min(20, len(cards))
    min_pe = min(10, len(cards))
    min_quant = min(20, len(cards))
    min_decision = min(20, len(cards))
    if cards and len(market_covered) < min_market:
        errors.append(f"strategy 候选 marketSnapshot 覆盖过低: {len(market_covered)}/{len(cards)}")
    if cards and len(pe_covered) < min_pe:
        errors.append(f"strategy 候选 PE 覆盖过低: {len(pe_covered)}/{len(cards)}")
    if cards and len(quant_covered) < min_quant:
        errors.append(f"strategy 候选 quantSnapshot 覆盖过低: {len(quant_covered)}/{len(cards)}")
    if cards and len(decision_covered) < min_decision:
        errors.append(f"strategy 候选 decisionProfile 覆盖过低: {len(decision_covered)}/{len(cards)}")
    decision_coverage = stock_cards.get("decisionCoverage") or {}
    if cards and decision_coverage.get("profiled", 0) < min_decision:
        errors.append("stockCards.decisionCoverage 缺失或覆盖过低")

    return not errors, errors, warnings


def _quote_rows(data: dict) -> list[dict]:
    rows: list[dict] = []
    for layer in data.get("layers") or []:
        rows.extend(layer.get("stocks") or [])
    rows.extend(data.get("etfs") or [])
    rows.extend(data.get("hk") or [])
    return rows


def validate_market() -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    data = _load("data.json")
    strategy = _load("strategy.json")

    layer_stocks = [
        stock
        for layer in data.get("layers") or []
        for stock in layer.get("stocks") or []
    ]
    etfs = data.get("etfs") or []
    hk = data.get("hk") or []
    if not layer_stocks or not etfs or not hk:
        errors.append("data.json 市场区块为空")

    rows = _quote_rows(data)
    missing_signal = [
        row.get("name") or row.get("code") or "unknown"
        for row in rows
        if not row.get("signal")
    ]
    if missing_signal:
        errors.append("data.json 信号字段缺失: " + ", ".join(missing_signal[:8]))

    quote_index: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("code") or "")
        if not code:
            continue
        quote_index[code] = row
        quote_index[code.replace("HK.", "")] = row

    cards = ((strategy.get("stockCards") or {}).get("cards") or {})
    for code, card in cards.items():
        snap = card.get("marketSnapshot")
        if snap:
            quote_index.setdefault(code, snap)
    covered = [code for code in cards if code in quote_index]
    with_pe = [
        code for code in covered
        if quote_index[code].get("pe") and quote_index[code].get("pe") != "-"
    ]
    if cards and len(covered) < max(1, min(8, len(cards))):
        warnings.append(f"strategy 候选行情覆盖偏低: {len(covered)}/{len(cards)}")
    if cards and not with_pe:
        warnings.append("strategy 候选暂无 PE 覆盖，价值结论只能停留在结构层")

    return not errors, errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="校验公开 H5 数据")
    parser.add_argument(
        "--scope",
        choices=["all", "strategy", "market"],
        default="all",
        help="校验范围",
    )
    args = parser.parse_args()

    checks = []
    if args.scope in ("all", "strategy"):
        checks.append(("strategy", validate_strategy()))
    if args.scope in ("all", "market"):
        checks.append(("market", validate_market()))

    ok = True
    for name, (passed, errors, warnings) in checks:
        if passed:
            print(f"[ok] {name}")
        else:
            ok = False
            for err in errors:
                print(f"[abort] {name}: {err}", file=sys.stderr)
        for warning in warnings:
            print(f"[warn] {name}: {warning}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
