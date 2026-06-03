#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
今日关注 · 日报数据生成器 (H5 首页卡片用)
================================================

【性质说明】
① 这是【公开策略日报】聚合器 —— 只聚合系统自动产出的策略摘要 (信号触发、卖水人
   环节结论、自选/融资热度等),【不是飞书群私聊内容的搬运】。所有 item 都是脱敏后的
   策略级摘要,面向公开看板 (H5 首页卡片)。

② 【将来若要并入"飞书群消息脱敏摘要"】:需另接安全的飞书读取 + 脱敏管线
   (去除人名/原话/截图/账号),再写入下方预留的 `group_digest` 字段位 (默认空 [])。
   本脚本【不读取任何飞书群原始消息】,group_digest 永远留空,留给未来安全管线填充。

【严禁 (公开安全红线)】
   - 不读 HOLDINGS.md 的持仓量 / holdings_snapshot 的 shares
   - 不读任何成本 / 盈亏 / 仓位金额
   - 不读 config_override / 密钥 / token / secret
   - 不读飞书群原始私聊消息
   只做"系统策略摘要"的聚合;取不到的源直接跳过,绝不编造。

【降级原则】
   每个源独立 try/except,任一源失败只跳过该源 (优雅降级);
   全部源都取不到 → available=false + "今日暂无摘要"。

CLI:  python3 daily-brief.py [--dry-run]
产出: 投资工作室H5/daily_brief.json
      {date, updateTime, items:[{type,title,detail,tag}...], available, group_digest}
"""

from __future__ import annotations

import json
import re
import sys
import collections
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---- 路径锚定 ----
# HERE = 脚本所在 H5 目录(产出落这里); WORKSPACE 固定指向引擎根(与 strategy-feed.py 一致),
# 这样脚本无论部署在哪个 H5 目录,引擎数据读取都稳定。
HERE = Path(__file__).resolve().parent          # H5 目录(产出 daily_brief.json 落这)
WORKSPACE = Path("/Users/long/.openclaw/workspace-main")  # 引擎根(固定)
OUT_PATH = HERE / "daily_brief.json"

# 公开安全源 (只读)
STRATEGY_JSON = HERE / "strategy.json"           # strategy-feed.py 的产出 (可能尚未生成)
WATER_SELLER_MD = WORKSPACE / "docs" / "WATER_SELLER_REPORTS.md"
WATER_SELLER_MD2 = WORKSPACE / "docs" / "WATER_SELLER_REPORTS_2.md"
SIGNALS_DIR = WORKSPACE / "scripts" / "data" / "signals"
WATCHLIST_JSON = WORKSPACE / "data" / "watchlist.json"
FUNDING_DB = WORKSPACE / "scripts" / "funding_tracker" / "funding.db"
FUNDING_SAMPLE = WORKSPACE / "scripts" / "funding_tracker" / "sample_rounds.json"

CN_TZ = timezone(timedelta(hours=8))

# 任何 item 文本里出现这些词 = 触碰红线,聚合前先扫一遍杜绝意外泄露
FORBIDDEN_SUBSTR = ["持仓", "shares", "成本", "盈亏", "token", "secret", "密钥"]


def _today() -> datetime:
    return datetime.now(CN_TZ)


def _safe_text(s: str) -> bool:
    """item 文本公开安全自检:命中任一红线词 → 不安全 (调用方应丢弃该 item)。"""
    low = (s or "").lower()
    for w in FORBIDDEN_SUBSTR:
        if w.lower() in low:
            return False
    return True


# ============================================================
# 源 1:美股 → A 股信号 (隔夜大涨 + 映射)
# 优先读 strategy.json 的 usMapping;取不到 (例如 strategy-feed 尚未跑) → 跳过
# ============================================================
def collect_us_mapping() -> list[dict]:
    items: list[dict] = []
    try:
        if not STRATEGY_JSON.exists():
            return items
        data = json.loads(STRATEGY_JSON.read_text(encoding="utf-8"))
        mapping = data.get("usMapping") or data.get("us_mapping") or []
        if isinstance(mapping, dict):
            mapping = mapping.get("items") or mapping.get("list") or []
        for m in mapping[:5]:
            if not isinstance(m, dict):
                continue
            us = m.get("us_name") or m.get("us") or m.get("ticker") or ""
            a = m.get("a_name") or m.get("a_share") or m.get("mapped") or ""
            chg = m.get("change") or m.get("pct") or m.get("overnight") or ""
            title = f"美股映射 · {us}".strip(" ·")
            detail = f"隔夜 {us} {chg} → 关注 A 股 {a}".strip()
            if not us and not a:
                continue
            items.append({
                "type": "us_mapping",
                "title": title or "美股→A股映射",
                "detail": detail,
                "tag": "隔夜映射",
            })
    except Exception:
        return []  # 优雅降级:整源跳过
    return items


# ============================================================
# 源 2:卖水人 Top 环节 (只取环节 + 证据等级,不搬细节)
# 解析 WATER_SELLER_REPORTS*.md 总结表里的 "卖水人 Top 环节" + "证据等级"
# ============================================================
def collect_water_seller() -> list[dict]:
    items: list[dict] = []
    seen = set()
    for md in (WATER_SELLER_MD, WATER_SELLER_MD2):
        try:
            if not md.exists():
                continue
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        # 找总结里的 "卖水人 Top 环节" 表格行:| 战役 | **环节** | **证据** | ... |
        # 该表特征:含 "卖水人 Top 环节" 表头后的数据行
        in_summary = False
        for line in text.splitlines():
            if "卖水人 Top 环节" in line and "|" in line:
                in_summary = True
                continue
            if in_summary:
                if not line.strip().startswith("|"):
                    if line.strip() == "":
                        continue
                    in_summary = False
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 3:
                    continue
                if set("".join(cells).replace("-", "")) <= set(" "):  # 分隔行 ---
                    continue
                if "战役" in cells[0] or "环节" in cells[1]:  # 表头
                    continue
                # cells: [战役, 卖水人Top环节, 证据等级, A股落点...]
                seg_raw = cells[1]
                seg = re.sub(r"\*\*|（.*?）|\(.*?\)", "", seg_raw).strip()
                seg = seg.strip("·* ")
                evid_raw = cells[2] if len(cells) > 2 else ""
                evid = re.sub(r"\*\*", "", evid_raw).strip()
                m = re.match(r"^([A-D](?:/[A-D])?)", evid)
                evid_grade = m.group(1) if m else (evid[:3] if evid else "?")
                if not seg or seg in seen:
                    continue
                seen.add(seg)
                items.append({
                    "type": "water_seller",
                    "title": f"卖水人环节 · {seg}",
                    "detail": f"持久卖水人环节,证据等级 {evid_grade}(只读研究,非交易信号)",
                    "tag": "产业链卖水人",
                })
        if items:
            break  # 第一份报告解析到即可,避免重复
    return items[:3]


# ============================================================
# 源 3:今日信号 (buy 类 / sell_warning)
# 只取 code/name/方向/触发,严禁任何持仓量
# ============================================================
def collect_today_signals() -> list[dict]:
    items: list[dict] = []
    today = _today()
    today_str = today.strftime("%Y-%m-%d")
    month_file = SIGNALS_DIR / f"signal_log_{today.strftime('%Y%m')}.jsonl"
    try:
        if not month_file.exists():
            return items
        buy_rows, sell_rows = [], []
        for ln in month_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            ts = o.get("ts", "")
            if not ts.startswith(today_str):
                continue
            stype = o.get("signal_type", "")
            direction = (o.get("direction") or "").lower()
            action = (o.get("advice") or {}).get("action", "")
            trigger = (o.get("context") or {}).get("trigger", "") or ""
            code = o.get("code", "")
            name = o.get("name", "")
            row = {"code": code, "name": name, "trigger": trigger, "dir": direction}
            # 卖出预警
            if stype == "sell_warning" or action == "exit" or direction == "short":
                sell_rows.append(row)
            # 买入/发现类提示
            elif stype == "discovery" or action == "add" or direction == "long":
                buy_rows.append(row)
        # 汇总成卡片 (聚合,不逐条暴露,且天然不含任何持仓)
        if buy_rows:
            names = "、".join(
                (r["name"] or r["code"]) for r in buy_rows[:4] if (r["name"] or r["code"])
            )
            extra = f" 等 {len(buy_rows)} 只" if len(buy_rows) > 4 else ""
            trig = next((r["trigger"] for r in buy_rows if r["trigger"]), "")
            detail = f"今日机会提示:{names}{extra}"
            if trig:
                detail += f"(触发:{trig})"
            items.append({
                "type": "signal_buy",
                "title": f"今日机会信号 {len(buy_rows)} 条",
                "detail": detail,
                "tag": "买入提示",
            })
        if sell_rows:
            names = "、".join(
                (r["name"] or r["code"]) for r in sell_rows[:4] if (r["name"] or r["code"])
            )
            extra = f" 等 {len(sell_rows)} 只" if len(sell_rows) > 4 else ""
            trig = next((r["trigger"] for r in sell_rows if r["trigger"]), "")
            detail = f"今日卖出预警:{names}{extra}"
            if trig:
                detail += f"(触发:{trig})"
            items.append({
                "type": "signal_sell",
                "title": f"今日卖出预警 {len(sell_rows)} 条",
                "detail": detail,
                "tag": "卖出预警",
            })
    except Exception:
        return []
    return items


# ============================================================
# 源 4:自选异动 / 融资热度 (有则取层热度)
# watchlist:取自选数量 + 板块构成;funding:取细分赛道融资热度 Top
# 均不涉及任何个人持仓
# ============================================================
def collect_watchlist_funding() -> list[dict]:
    items: list[dict] = []
    # --- 自选异动:统计自选规模 + 今日异动数 ---
    try:
        if WATCHLIST_JSON.exists():
            wl = json.loads(WATCHLIST_JSON.read_text(encoding="utf-8"))
            buckets = wl.get("watchlist", {})
            total = 0
            cats = []
            for k, v in buckets.items():
                if isinstance(v, list):
                    total += len(v)
                    cats.append((k, len(v)))
            if total > 0:
                # 今日异动 (anomaly) 数 —— 关联自选热度,只取计数
                anomaly_n = _today_anomaly_count()
                cat_str = "、".join(f"{k}{n}" for k, n in cats if n)
                detail = f"自选池 {total} 只({cat_str})"
                if anomaly_n:
                    detail += f";今日异动 {anomaly_n} 次"
                items.append({
                    "type": "watchlist",
                    "title": "自选池异动",
                    "detail": detail,
                    "tag": "自选监控",
                })
    except Exception:
        pass

    # --- 融资热度:细分赛道融资笔数 Top (db 优先,回退 sample) ---
    try:
        heat = _funding_heat()
        if heat:
            top = "、".join(f"{seg}({n})" for seg, n in heat[:3])
            items.append({
                "type": "funding",
                "title": "一级融资热度",
                "detail": f"近期融资活跃赛道:{top}",
                "tag": "融资热度",
            })
    except Exception:
        pass
    return items


def _today_anomaly_count() -> int:
    today = _today()
    month_file = SIGNALS_DIR / f"signal_log_{today.strftime('%Y%m')}.jsonl"
    today_str = today.strftime("%Y-%m-%d")
    n = 0
    try:
        if not month_file.exists():
            return 0
        for ln in month_file.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if not o.get("ts", "").startswith(today_str):
                continue
            if o.get("signal_type", "").startswith("anomaly"):
                n += 1
    except Exception:
        return 0
    return n


def _funding_heat() -> list[tuple[str, int]]:
    """返回 [(细分赛道, 笔数)] 按热度降序。db 优先,回退 sample_rounds.json。"""
    rounds: list[dict] = []
    # 1) 真实 db
    try:
        if FUNDING_DB.exists():
            import sqlite3
            con = sqlite3.connect(str(FUNDING_DB))
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT sub_sector FROM funding_rounds"
                ).fetchall()
                rounds = [dict(r) for r in rows]
            finally:
                con.close()
    except Exception:
        rounds = []
    # 不脑补: 无真实融资数据则返回空(对外显"暂无"), 绝不用 sample 演示数据冒充真实推上 H5
    if not rounds:
        return []
    c = collections.Counter(
        (r.get("sub_sector") or "").strip() for r in rounds if (r.get("sub_sector") or "").strip()
    )
    return c.most_common()


# ============================================================
# 主聚合
# ============================================================
def build() -> dict:
    now = _today()
    all_items: list[dict] = []
    for collector in (
        collect_us_mapping,
        collect_water_seller,
        collect_today_signals,
        collect_watchlist_funding,
    ):
        try:
            for it in collector():
                # 公开安全自检:任何 item 文本命中红线词 → 丢弃
                blob = f"{it.get('title','')} {it.get('detail','')} {it.get('tag','')}"
                if _safe_text(blob):
                    all_items.append(it)
        except Exception:
            continue  # 单源失败不影响整体

    available = len(all_items) > 0
    if not available:
        all_items = [{
            "type": "empty",
            "title": "今日暂无摘要",
            "detail": "今日暂无可聚合的公开策略摘要",
            "tag": "",
        }]

    return {
        "date": now.strftime("%Y-%m-%d"),
        "updateTime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "items": all_items,
        "available": available,
        # 预留位:未来接入"飞书群消息脱敏摘要"安全管线后填充,当前永远空
        "group_digest": [],
    }


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    brief = build()
    payload = json.dumps(brief, ensure_ascii=False, indent=2)

    if dry:
        print("[dry-run] 不写文件,以下为将产出的 daily_brief.json:")
        print(payload)
        return 0

    # 原子写: 写临时文件再 rename, 防中断写出半截 JSON
    import os as _os
    _tmp = OUT_PATH.with_name(OUT_PATH.name + ".tmp")
    _tmp.write_text(payload + "\n", encoding="utf-8")
    _os.replace(_tmp, OUT_PATH)
    print(f"[ok] 已写出 {OUT_PATH}")
    print(f"[ok] available={brief['available']}  items={len(brief['items'])}")
    for it in brief["items"]:
        print(f"  - [{it['type']}] {it['title']} | {it['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
