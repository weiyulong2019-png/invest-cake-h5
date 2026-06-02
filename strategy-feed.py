#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strategy-feed.py — 投资工作室H5「策略看板」数据生成器（公开数据，只读调用）

定位：群聊无安全验证、无个人持仓数量的【数据策略看板】。
  本脚本只输出【公开策略数据】（产业链结构、共识态、公开行情、聚合胜率），
  绝不输出任何个人持仓数量/成本/盈亏，也绝不读取任何密钥。

它只读调用 workspace-main 新引擎的【模块函数 / CLI】（不改其源码、不写其数据、不推送）：
  - opportunity_tree.py   产业链树 + 非共识机会
  - us_ash_mapping.py     美股→A股映射（中文名/涨幅/状态/≥阈值大涨的A股链）
  - funding_tracker        一级市场融资热度（层分布；db 空则空）
  - signal_tracker.py      各 scanner 聚合胜率（公开数字，无个人数据）

降级原则：任何引擎取不到数据 → 该区块标 available=False / 空列表，
  绝不编造数字、绝不硬解析散文（如 WATER_SELLER_REPORTS*.md 无结构化源即跳过）。

输出：同目录下 strategy.json（公开看板用）。

CLI:
  python3 strategy-feed.py              # best-effort（尝试取行情/成交额算共识）
  python3 strategy-feed.py --dry-run    # 纯离线（不联网，共识态全降级 unknown）
  python3 strategy-feed.py --chain ai_server --threshold 10

部署提示（不在此脚本里改任何调度）：
  本脚本是独立的【新增一层】，不触碰现有 refresh-data.py / data.json 管道。
  如需自动刷新，请由运维另行在 auto-refresh.sh / launchd / Actions 里加一条
  `python3 strategy-feed.py`（建议盘后/低频，产业链与融资数据变动慢）。
  —— 本脚本绝不自行修改 launchd/cron/Actions。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT_FILE = HERE / "strategy.json"

# workspace-main 引擎路径（只读 import）。固定相对定位，找不到则该区块降级。
WORKSPACE_SCRIPTS = Path("/Users/long/.openclaw/workspace-main/scripts")

# P0 数据模块的产出目录（本脚本【只读】这些 JSON，绝不运行/修改其源码）。
#   美股情报: scripts/data/us_intel/  (sec_edgar / us_quote / 13F / 社媒情绪)
#   宏观情报: scripts/data/intel/     (rss_intel / polymarket_odds)
US_INTEL_DIR = WORKSPACE_SCRIPTS / "data" / "us_intel"
INTEL_DIR = WORKSPACE_SCRIPTS / "data" / "intel"

# 上游降级骨架里出现这些 status 即视为「未取到」（不当成真数据）。
_DEGRADED_STATUS = {"degraded", "error", "parse_error", "empty"}

DEFAULT_CHAIN = "ai_server"
DEFAULT_THRESHOLD = 10.0

# 全部产业链（key → 看板显示名）。key 必须存在于 opportunity_tree.CHAIN_FILES。
CHAINS_ALL = [
    ("ai_server", "AI服务器"),
    ("humanoid_robot", "人形机器人"),
    ("power_grid", "电网"),
    ("new_energy", "新能源"),
    ("commercial_space", "商业航天"),
    ("innovative_drug", "创新药"),
]


def _ensure_workspace_on_path() -> bool:
    """把 workspace scripts 目录放上 sys.path 以便 import 引擎模块。"""
    if WORKSPACE_SCRIPTS.is_dir():
        p = str(WORKSPACE_SCRIPTS)
        if p not in sys.path:
            sys.path.insert(0, p)
        return True
    return False


# ───────────────────────── 1) 产业链树 / 非共识机会 ─────────────────────────
def build_chain_tree(chain: str, dry_run: bool) -> dict:
    """调 opportunity_tree（只读图谱+mapping），输出环节+标的+共识态。
    取不到 → available=False。"""
    block = {"available": False, "note": "暂无数据", "chain": chain,
             "entry": None, "stats": {}, "segments": [], "non_consensus": [],
             "upstream_todo": []}
    if not _ensure_workspace_on_path():
        block["note"] = "暂无数据（引擎目录不可达）"
        return block
    try:
        import opportunity_tree as ot  # type: ignore
    except Exception as exc:
        block["note"] = f"暂无数据（opportunity_tree 不可用: {exc})"
        return block
    try:
        graph = ot.load_graph(chain)
        theme = ot.CHAIN_THEME.get(chain, graph.get("theme", ""))
        mapping = ot.load_mapping(theme)
        tree_nodes = graph.get("tree", {})
        entry = next((n for n, v in tree_nodes.items() if v.get("tier") == 0), None)
        if entry is None:
            segs = graph.get("segments") or list(tree_nodes)
            entry = segs[0] if segs else None
        if entry is None:
            block["note"] = "暂无数据（图谱无可用入口环节）"
            return block

        root = ot.build_tree(graph, entry)

        turnover: dict = {}
        consensus_live = False
        if not dry_run:
            all_codes = sorted({c for peers in mapping.values() for c in peers})
            turnover = ot.fetch_turnover(all_codes) or {}
            consensus_live = bool(turnover)
        ot.annotate_consensus(root, mapping, turnover)

        acc = {"total_nodes": 0, "nodes_with_targets": 0, "nodes_todo": 0,
               "all_targets": [], "non_consensus": [], "upstream_todo": []}
        ot.collect_stats(root, acc)

        # 扁平化为前端友好的环节列表（只取有标的的环节，控制体积）
        segs_out: list = []

        def _walk(node: dict, depth: int):
            holds = node.get("holdings", []) or []
            if holds:
                segs_out.append({
                    "name": node["name"],
                    "tier": node.get("tier"),
                    "depth": depth,
                    "kind": node.get("kind"),
                    "holdings": [{
                        "code": h["code"],
                        "name": h["name"],
                        "is_leader": bool(h.get("is_leader")),
                        "consensus_state": h.get("consensus_state", "unknown"),
                    } for h in holds],
                })
            for ch in node.get("children", []) or []:
                _walk(ch, depth + 1)

        _walk(root, 0)

        block.update({
            "available": True,
            "note": "",
            "theme": theme,
            "entry": entry,
            "consensus_live": consensus_live,
            "stats": {
                "total_nodes": acc["total_nodes"],
                "nodes_with_targets": acc["nodes_with_targets"],
                "nodes_todo": acc["nodes_todo"],
                "total_targets": len(acc["all_targets"]),
            },
            "segments": segs_out[:40],  # 控制 payload 体积
            "non_consensus": [{
                "code": r["code"], "name": r["name"], "segment": r["segment"],
                "tier": r.get("tier"), "tree_path": r.get("tree_path"),
            } for r in acc["non_consensus"]][:20],
            "upstream_todo": [{
                "segment": u["segment"], "tier": u.get("tier"),
            } for u in acc["upstream_todo"]][:20],
        })
        return block
    except Exception as exc:
        block["note"] = f"暂无数据（产业链树构建失败: {exc})"
        return block


def build_all_chains(dry_run: bool) -> dict:
    """循环全部产业链，输出 chains:{key:block} + chainOrder + defaultChain。
    每条链复用 build_chain_tree（任意 chain 通用）。单链失败不影响其它（块内已 try）。
    新链若 a_share_proposed 为占位 → 该链 segments 空（诚实"图谱待补"，不脑补）。"""
    chains: dict = {}
    order: list = []
    for key, label in CHAINS_ALL:
        try:
            blk = build_chain_tree(key, dry_run)
        except Exception as exc:  # 兜底，单链异常不拖垮整体
            blk = {"available": False, "note": f"暂无数据（{exc}）", "chain": key,
                   "segments": [], "non_consensus": [], "upstream_todo": [], "stats": {}}
        blk["key"] = key
        blk["label"] = label
        chains[key] = blk
        order.append(key)
    return {"chains": chains, "chainOrder": order, "defaultChain": DEFAULT_CHAIN}


# ─────────────────── P0 情报数据：只读 us_intel / intel 产出 ───────────────────
def _read_json_safe(path: Path) -> dict | None:
    """只读一个 JSON 文件；不存在/解析失败 → None（不抛、不编造）。"""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _is_ok(payload: dict | None) -> bool:
    """上游 payload 是否为有效（非降级）数据。"""
    if not isinstance(payload, dict):
        return False
    return str(payload.get("status", "ok")).lower() not in _DEGRADED_STATUS


def summarize_insider(ticker: str) -> dict:
    """读 data/us_intel/sec_all_<T>.json 的 Form4 内部人交易 → 公开安全摘要。

    只输出聚合方向与笔数（买/卖/授予笔数、最近一笔方向/日期/角色），
    **绝不输出任何个人持仓量/成本**（这是公开看板）。模块未产出 → available=False。
    """
    out = {"available": False, "note": "未取到", "buys": 0, "sells": 0,
           "grants": 0, "net_dir": None, "latest": None}
    safe = "".join(c if c.isalnum() else "_" for c in ticker)
    payload = _read_json_safe(US_INTEL_DIR / f"sec_all_{safe}.json")
    if payload is None:
        payload = _read_json_safe(US_INTEL_DIR / f"sec_insiders_{safe}.json")
        ins = payload if _is_ok(payload) else None
    else:
        ins = (payload.get("parts", {}) or {}).get("insiders")
    if not _is_ok(ins):
        return out
    filings = ins.get("insider_filings") or []
    if not filings:
        out.update({"available": True, "note": "近期无内部人交易记录"})
        return out
    buys = sells = grants = 0
    latest = None
    for f in filings:
        for t in f.get("transactions") or []:
            code = (t.get("code") or "").upper()
            acq = (t.get("acquired_disposed") or "").upper()
            # P=公开市场买入, S=卖出, A=授予, M=期权行权; 以 A/D 兜底方向
            if code == "P" or (code not in ("S",) and acq == "A" and code != "A"):
                buys += 1
            elif code == "S" or acq == "D":
                sells += 1
            elif code == "A":
                grants += 1
        if latest is None and (f.get("transactions") or []):
            t0 = f["transactions"][0]
            latest = {
                "owner": f.get("owner"),
                "relationship": f.get("relationship"),
                "date": t0.get("date") or f.get("filing_date"),
                "dir": "买入" if (t0.get("code") or "").upper() == "P"
                       else ("卖出" if (t0.get("code") or "").upper() == "S"
                             else "授予/其它"),
            }
    net = None
    if buys or sells:
        net = "净买入" if buys > sells else ("净卖出" if sells > buys else "买卖均衡")
    out.update({"available": True, "note": "",
                "buys": buys, "sells": sells, "grants": grants,
                "net_dir": net, "latest": latest})
    return out


def summarize_institution(ticker: str) -> dict:
    """13F 机构持仓（按持有标的反查）。

    当前 us_intel/sec_edgar 的 13F 接口以【机构 CIK】为键，并非按被持标的反查，
    故无现成 per-ticker 机构持仓产出 → 诚实标 available=False / 未取到（不脑补）。
    若未来上游产出 data/us_intel/inst_by_ticker_<T>.json（含 holders 字段）则自动接上。
    """
    out = {"available": False, "note": "未取到", "holders": []}
    safe = "".join(c if c.isalnum() else "_" for c in ticker)
    payload = _read_json_safe(US_INTEL_DIR / f"inst_by_ticker_{safe}.json")
    if not _is_ok(payload):
        return out
    holders = payload.get("holders") or payload.get("top_holders") or []
    if not holders:
        out["note"] = "暂无 13F 持仓记录"
        return out
    out.update({"available": True, "note": "",
                "holders": [{"name": h.get("filer") or h.get("name"),
                             "shares": h.get("shares"),
                             "value_usd_k": h.get("value_usd_k")}
                            for h in holders[:5]]})
    return out


def summarize_sentiment(ticker: str) -> dict:
    """散户情绪（StockTwits / Reddit）。

    上游 us_social_sentiment 模块若未产出 → available=False / 未取到（不编造）。
    约定输出 data/us_intel/social_sentiment.json，形如:
      {"status":"ok","tickers":{"NVDA":{"score":..,"bullish_pct":..,"msg_count":..,
                                        "source":"stocktwits","trend":".."}}}
    """
    out = {"available": False, "note": "未取到"}
    payload = _read_json_safe(US_INTEL_DIR / "social_sentiment.json")
    if not _is_ok(payload):
        return out
    row = (payload.get("tickers", {}) or {}).get(ticker.upper())
    if not isinstance(row, dict) or str(row.get("status", "ok")).lower() in _DEGRADED_STATUS:
        return out
    out.update({"available": True, "note": "",
                "score": row.get("score"),
                "bullish_pct": row.get("bullish_pct"),
                "msg_count": row.get("msg_count"),
                "source": row.get("source"),
                "trend": row.get("trend")})
    return out


def build_intel(top_rss: int = 12, top_poly: int = 2) -> dict:
    """📰 情报速递块：聚合 RSS 最新头条 + Polymarket 事件赔率（皆为公开数据）。

    只读 scripts/data/intel/{rss_latest,polymarket_latest}.json；
    任一未产出/降级 → 该子块标 available=False / 未取到，整块不报错。
    """
    block = {"available": False, "note": "未取到",
             "rss": {"available": False, "note": "未取到", "generated_at": None, "items": []},
             "polymarket": {"available": False, "note": "未取到", "generated_at": None, "events": []}}

    # ── RSS 头条（按源聚合后取最新 N 条，保留来源与证据等级）──
    rss = _read_json_safe(INTEL_DIR / "rss_latest.json")
    if isinstance(rss, dict) and rss.get("feeds"):
        items = []
        for feed in rss.get("feeds", []):
            if feed.get("status") != "ok":
                continue
            for it in feed.get("items", [])[:3]:  # 每源最多 3 条，保多样性
                items.append({
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "published": it.get("published", ""),
                    "source": feed.get("name") or feed.get("source_org") or feed.get("rss_id"),
                    "category": feed.get("category", ""),
                    "evidence_grade": feed.get("evidence_grade", ""),
                })
        if items:
            block["rss"] = {
                "available": True, "note": "",
                "generated_at": rss.get("generated_at"),
                "ok_count": rss.get("ok_count"),
                "source_count": rss.get("source_count"),
                "items": items[:top_rss],
            }
        else:
            block["rss"]["note"] = "RSS 源均降级/无条目"

    # ── Polymarket 赔率（每主题取成交额最高的 N 个事件）──
    poly = _read_json_safe(INTEL_DIR / "polymarket_latest.json")
    if isinstance(poly, dict) and not poly.get("degraded") and poly.get("themes"):
        events = []
        for theme, markets in poly.get("themes", {}).items():
            if not markets:
                continue
            ranked = sorted(markets, key=lambda m: m.get("volume_usdc") or 0, reverse=True)
            for m in ranked[:top_poly]:
                prob = m.get("implied_prob")
                events.append({
                    "theme": theme,
                    "market": m.get("market", ""),
                    "outcome": m.get("outcome", ""),
                    "implied_prob": prob,
                    "implied_pct": round(prob * 100, 1) if isinstance(prob, (int, float)) else None,
                    "change_24h": m.get("change_24h"),
                    "volume_usdc": m.get("volume_usdc"),
                    "end_date": m.get("end_date"),
                })
        if events:
            block["polymarket"] = {
                "available": True, "note": poly.get("note", ""),
                "generated_at": poly.get("asof"),
                "market_count": poly.get("market_count"),
                "events": events,
            }
        else:
            block["polymarket"]["note"] = "无可用事件"
    elif isinstance(poly, dict) and poly.get("degraded"):
        block["polymarket"]["note"] = "Polymarket 取数降级（未取到）"

    block["available"] = block["rss"]["available"] or block["polymarket"]["available"]
    if block["available"]:
        block["note"] = ""
    return block


# ───────────────────────── 2) 美股→A股映射 ─────────────────────────
def build_us_mapping(chain: str, threshold: float, dry_run: bool) -> dict:
    """调 us_ash_mapping：中文名/涨幅/状态/≥阈值大涨的A股链。
    行情取不到 → 列出锚但 change=None（标"行情未取到"），不编数字。"""
    block = {"available": False, "note": "暂无数据", "chain": chain,
             "threshold": threshold, "quote_ok": False, "anchors": [],
             "big_movers": []}
    if not _ensure_workspace_on_path():
        block["note"] = "暂无数据（引擎目录不可达）"
        return block
    try:
        import us_ash_mapping as um  # type: ignore
    except Exception as exc:
        block["note"] = f"暂无数据（us_ash_mapping 不可用: {exc})"
        return block
    try:
        anchors_data = um.load_anchors()
        anchors = anchors_data.get("anchors", {})
        chain = anchors_data.get("chain", chain)
        tickers = list(anchors.keys())

        quotes: dict = {}
        if not dry_run and tickers:
            try:
                quotes = um.fetch_us_quotes(tickers) or {}
            except Exception:
                quotes = {}
        quote_ok = bool(quotes)

        anchor_rows = []
        big = []
        for ticker, meta in anchors.items():
            q = quotes.get(ticker)
            chg = q["change_rate_last"] if q else None
            status = um.classify_status(q["closes"]) if q else "行情未取到"
            cn = meta.get("cn_name", ticker)
            row = {
                "ticker": ticker,
                "cn_name": cn,
                "change": round(chg, 2) if isinstance(chg, (int, float)) else None,
                "status": status,
                "big_mover": bool(chg is not None and chg >= threshold),
                # P0 增强：内部人(Form4) / 机构(13F) / 散户情绪。模块未产出则各自 available=False。
                # 全程只读公开数据，绝不含个人持仓量。
                "insider": summarize_insider(ticker),
                "institution": summarize_institution(ticker),
                "sentiment": summarize_sentiment(ticker),
            }
            anchor_rows.append(row)
            if row["big_mover"]:
                big.append((ticker, meta, chg))

        # 有行情的按涨幅降序在前
        anchor_rows.sort(key=lambda r: (r["change"] is None,
                                        -(r["change"] if r["change"] is not None else 0)))

        # 大涨股 → A股链映射（只读图谱）
        movers_out = []
        for ticker, meta, chg in big:
            cn = meta.get("cn_name", ticker)
            anchor_seg = meta.get("chain_anchor_segment", "")
            entry = {"ticker": ticker, "cn_name": cn,
                     "change": round(chg, 2), "anchor_segment": anchor_seg,
                     "segments": [], "note": ""}
            if not anchor_seg:
                entry["note"] = "锚环节未对齐图谱，待确认"
                movers_out.append(entry)
                continue
            res = um.map_to_ashare(chain, anchor_seg, dry_run)
            if not res or res.get("_error"):
                entry["note"] = (res or {}).get("_error", "链映射不可用")
                movers_out.append(entry)
                continue
            flat: list = []
            um.flatten_segments(res["root"], flat)
            seg_list = []
            for s in flat:
                holds = s.get("holdings", []) or []
                if not holds:
                    continue
                seg_list.append({
                    "name": s["name"],
                    "tier": s.get("tier"),
                    "holdings": [{
                        "code": h["code"], "name": h["name"],
                        "is_leader": bool(h.get("is_leader")),
                        "consensus_state": h.get("consensus_state", "unknown"),
                    } for h in holds],
                })
            entry["segments"] = seg_list[:12]
            entry["stats"] = {
                "nodes_with_targets": res["stats"]["nodes_with_targets"],
                "total_targets": len(res["stats"]["all_targets"]),
            }
            movers_out.append(entry)

        block.update({
            "available": True,
            "note": "" if quote_ok else "行情未取到（离线/外网不可用）",
            "chain": chain,
            "quote_ok": quote_ok,
            "anchors": anchor_rows,
            "big_movers": movers_out,
        })
        return block
    except Exception as exc:
        block["note"] = f"暂无数据（美股映射失败: {exc})"
        return block


# ───────────────────────── 3) 融资热度（层分布） ─────────────────────────
def build_funding(months: int = 12) -> dict:
    """调 funding_tracker：一级市场融资层分布。db 空 → 空列表。
    只输出聚合（事件数/已披露总额/占比/中位估值），无个人数据。"""
    block = {"available": False, "note": "暂无数据", "months": months,
             "total_events": 0, "layers": []}
    if not _ensure_workspace_on_path():
        block["note"] = "暂无数据（引擎目录不可达）"
        return block
    try:
        from datetime import timedelta
        from funding_tracker.db import FundingDB  # type: ignore
        from funding_tracker.classify import LAYERS, LAYER_LABELS, fmt_usd_m  # type: ignore
    except Exception as exc:
        block["note"] = f"暂无数据（funding_tracker 不可用: {exc})"
        return block
    try:
        since = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
        with FundingDB() as db:
            rows = db.query(since=since, order="date ASC")
        if not rows:
            block["available"] = True  # 引擎在线，只是无数据
            block["note"] = "一级市场融资库暂无记录"
            return block

        from collections import defaultdict
        by_layer = defaultdict(list)
        for r in rows:
            by_layer[r["layer"]].append(r)
        total_amt = sum(r["amount_usd_m"] or 0 for r in rows)
        hot_since = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

        layers_out = []
        for layer in LAYERS:
            lr = by_layer.get(layer)
            if not lr:
                continue
            amt = sum(r["amount_usd_m"] or 0 for r in lr)
            vals = sorted(r["valuation_usd_m"] for r in lr if r["valuation_usd_m"])
            med_val = vals[len(vals) // 2] if vals else None
            share = (amt / total_amt) if total_amt else 0
            recent = [r for r in lr if (r["date"] or "") >= hot_since]
            heat = min(5, 1 + len(recent) // 2) if recent else 0
            layers_out.append({
                "layer": layer,
                "label": LAYER_LABELS.get(layer, layer),
                "events": len(lr),
                "amount_usd_m": round(amt, 1),
                "amount_display": fmt_usd_m(amt),
                "share_pct": round(share * 100, 1),
                "median_val_display": fmt_usd_m(med_val),
                "heat": heat,
                "recent_90d_events": len(recent),
            })

        block.update({
            "available": True, "note": "",
            "total_events": len(rows),
            "total_amount_display": fmt_usd_m(total_amt),
            "layers": layers_out,
        })
        return block
    except Exception as exc:
        block["note"] = f"暂无数据（融资热度失败: {exc})"
        return block


# ───────────────────────── 4) 各 scanner 聚合胜率 ─────────────────────────
def build_win_rate(min_samples: int = 5) -> dict:
    """调 signal_tracker：按 agent 切聚合胜率。纯聚合数字，无个人持仓数据。"""
    block = {"available": False, "note": "暂无数据", "min_samples": min_samples,
             "scanners": []}
    if not _ensure_workspace_on_path():
        block["note"] = "暂无数据（引擎目录不可达）"
        return block
    try:
        from signal_tracker import get_tracker  # type: ignore
    except Exception as exc:
        block["note"] = f"暂无数据（signal_tracker 不可用: {exc})"
        return block
    try:
        t = get_tracker()
        stats = t.stats_by("agent", min_samples=min_samples)
        scanners = []
        for name, s in stats.items():
            scanners.append({
                "scanner": name,
                "count": s.get("count", 0),
                "decided": s.get("decided", 0),
                "win_rate": s.get("win_rate"),  # None 表示样本不足/不参与裁决
                "avg_ret_5d": s.get("avg_ret_5d"),
                "note": s.get("note", ""),
            })
        # 已裁决多的在前
        scanners.sort(key=lambda x: (-(x["decided"] or 0), x["scanner"]))
        block.update({
            "available": True,
            "note": "" if scanners else "暂无信号样本",
            "scanners": scanners,
        })
        return block
    except Exception as exc:
        block["note"] = f"暂无数据（胜率统计失败: {exc})"
        return block


# ───────────────────────── 5) 卖水人（仅结构化源） ─────────────────────────
def build_water_sellers() -> dict:
    """卖水人 Top 环节。仅当存在【结构化源】时才输出；
    现状只有 docs/WATER_SELLER_REPORTS*.md 散文（无结构化 JSON）→ 按铁律跳过，不硬解析散文。"""
    return {
        "available": False,
        "note": "暂无结构化数据源（卖水人报告为散文，按铁律不硬解析）",
        "items": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="投资工作室H5 策略看板数据生成（公开数据）")
    ap.add_argument("--dry-run", action="store_true",
                    help="纯离线：不联网取行情/成交额，共识态全降级 unknown")
    ap.add_argument("--chain", default=DEFAULT_CHAIN, help="产业链（默认 ai_server）")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="美股大涨触发A股链映射的阈值%%（默认 10）")
    args = ap.parse_args()

    now = datetime.now()
    print("=== 投资工作室H5 策略看板数据生成 ===")
    print(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'DRY-RUN(离线)' if args.dry_run else 'LIVE(best-effort)'}  chain={args.chain}  阈值≥{args.threshold:.0f}%")
    print(f"引擎目录: {WORKSPACE_SCRIPTS}  ({'可达' if WORKSPACE_SCRIPTS.is_dir() else '不可达'})")
    print()

    print("[1/5] 产业链树 / 非共识机会（全部 6 链）...")
    all_chains = build_all_chains(args.dry_run)
    chain_tree = all_chains["chains"].get(args.chain) or build_chain_tree(args.chain, args.dry_run)
    for k in all_chains["chainOrder"]:
        b = all_chains["chains"][k]
        print(f"      {'OK' if b['available'] else 'SKIP'} · {b.get('label', k):10} "
              f"环节 {len(b.get('segments', []))} · 非共识 {len(b.get('non_consensus', []))} · {b.get('note') or '—'}")

    print("[2/5] 美股→A股映射 ...")
    us_mapping = build_us_mapping(args.chain, args.threshold, args.dry_run)
    print(f"      {'OK' if us_mapping['available'] else 'SKIP'} · "
          f"锚 {len(us_mapping.get('anchors', []))} · "
          f"大涨 {len(us_mapping.get('big_movers', []))} · "
          f"行情{'有' if us_mapping.get('quote_ok') else '无'} · {us_mapping.get('note') or '—'}")

    print("[3/5] 融资热度（层分布） ...")
    funding = build_funding()
    print(f"      {'OK' if funding['available'] else 'SKIP'} · "
          f"层 {len(funding.get('layers', []))} · 事件 {funding.get('total_events', 0)} · {funding.get('note') or '—'}")

    print("[4/5] 各 scanner 聚合胜率 ...")
    win_rate = build_win_rate()
    print(f"      {'OK' if win_rate['available'] else 'SKIP'} · "
          f"scanner {len(win_rate.get('scanners', []))} · {win_rate.get('note') or '—'}")

    print("[5/6] 卖水人（仅结构化源） ...")
    water = build_water_sellers()
    print(f"      {'OK' if water['available'] else 'SKIP'} · {water.get('note') or '—'}")

    print("[6/6] 情报速递（RSS 头条 + Polymarket 赔率，只读 P0 产出） ...")
    intel = build_intel()
    print(f"      {'OK' if intel['available'] else 'SKIP'} · "
          f"RSS {len(intel['rss'].get('items', []))} 条 / "
          f"Polymarket {len(intel['polymarket'].get('events', []))} 事件 · "
          f"{intel.get('note') or '—'}")
    # 美股锚增强字段命中情况（只统计，不打印任何持仓量）
    enr = [a for a in us_mapping.get("anchors", []) if a.get("insider", {}).get("available")]
    print(f"      美股锚内部人增强命中: {len(enr)}/{len(us_mapping.get('anchors', []))}")

    out = {
        "updateTime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(now.timestamp()),
        "mode": "dry-run" if args.dry_run else "live",
        "disclaimer": "公开策略数据看板，仅含聚合/结构化信息，不构成投资建议。",
        "waterSellers": water,
        "chainTree": chain_tree,           # 向后兼容：默认链（ai_server）
        "chains": all_chains["chains"],     # 多链：横向 tab 切换用
        "chainOrder": all_chains["chainOrder"],
        "defaultChain": all_chains["defaultChain"],
        "usMapping": us_mapping,
        "funding": funding,
        "winRate": win_rate,
        "intel": intel,                     # 📰 情报速递：RSS 头条 + Polymarket 赔率
    }

    OUTPUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"✅ 已写入: {OUTPUT_FILE}")
    available = [k for k in ("waterSellers", "chainTree", "usMapping", "funding", "winRate", "intel")
                 if out[k].get("available")]
    print(f"   可用区块: {', '.join(available) if available else '（全部降级，无可用区块）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
