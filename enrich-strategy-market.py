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
    STRATEGY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] strategy market snapshots: {enhanced}/{len(a_cards)}, PE {pe_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
