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

# 持仓清单（只读，仅用于提取「持仓 ETF 代码」给公开看板排序用）。
#   铁律：只取代码，绝不输出任何持仓量/成本/盈亏。
HOLDINGS_MD = Path("/Users/long/.openclaw/workspace-main/HOLDINGS.md")

# 全部产业链（key → 看板显示名）。key 必须存在于 opportunity_tree.CHAIN_FILES。
# 链谱(2026-06-04 首页重构):
#   - 首页 tab 按独立产业链展示；new_energy 恢复为独立链。
#   - ai_server 仍保留能源层以表达 AI 数据中心电力约束；新能源 tab 用独立图谱展开。
#   - humanoid_robot(人形机器人) 重命名扩展为 physical_ai(物理AI: 本体+具身大脑+自动驾驶)。
CHAINS_ALL = [
    ("ai_server", "AI服务器"),
    ("new_energy", "新能源"),
    ("commercial_space", "商业航天"),
    ("physical_ai", "物理AI"),
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
            "segments": segs_out[:60],  # 控制 payload 体积(ai_server 并入能源层后环节增多, 上调至 60)
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


# ─────────────────── 情报中文化（intel 翻译层） ───────────────────
# RSS 标题/来源 + Polymarket 事件名为英文上游数据；下列纯函数将其改写为中文。
# 规则化模板覆盖高频句式（Fed 利率 / 通胀阈值 / 衰退等），未覆盖者保留原文并标 [机器未译]，不脑补含义。
import re as _re

# Polymarket 主题 → 中文标签
_POLY_THEME_CN = {
    "fed_rates": "美联储利率", "inflation": "通胀", "ai": "人工智能",
    "geopolitics": "地缘政治", "election": "选举", "tech_corp": "科技公司",
    "crypto": "加密货币",
}

# RSS 来源机构名 → 中文
_RSS_SOURCE_CN = {
    "SEC Press Releases": "美国证监会(SEC)新闻",
    "Federal Reserve Press": "美联储新闻",
    "Treasury Press Releases": "美国财政部新闻",
    "CFTC Press Releases": "美国商品期货委员会(CFTC)新闻",
    "BLS News Releases": "美国劳工统计局(BLS)新闻",
    "ECB Press": "欧洲央行(ECB)新闻",
}

# RSS 标题高频短语 → 中文（列表顺序=替换优先级，长短语在前避免子串误伤）
_RSS_PHRASE_CN = [
    ("Federal Reserve Board issues enforcement actions with", "美联储理事会对以下对象采取执法行动："),
    ("member of the Board of Governors of the Federal Reserve System", "美联储理事会理事"),
    ("Securities and Exchange Commission", "美国证券交易委员会"),
    ("Investor Advisory Committee", "投资者咨询委员会"),
    ("Climate-Related Disclosure Rules", "气候相关信息披露规则"),
    ("Board of Governors", "理事会"),
    ("Federal Reserve System", "美联储系统"),
    ("Federal Reserve Board", "美联储理事会"),
    ("discount rate meeting", "贴现率会议"),
    ("enforcement actions", "执法行动"),
    ("takes oath of office", "宣誓就职"),
    ("Press Release", "新闻稿"),
    ("Announces", "宣布"),
    ("Proposes", "提议"),
    ("Rescission", "撤销"),
    ("Minutes of the", "纪要："),
    ("New Members", "新成员"),
    ("Four", "四名"),
    ("to Host", "将主办"),
    ("Meeting", "会议"),
    ("chairman", "主席"),
    ("former employee", "前雇员"),
    (" on ", " 时间："),
    (" and ", "、"),
    (" of the ", " 之"),
    (" of ", " 的"),
    (" with ", " 与 "),
    (" as ", " 任 "),
    (" a member", "成员"),
]


def _translate_rss_title(title: str) -> str:
    """RSS 英文标题 → 中文（高频短语替换）。无任何英文短语命中且仍含大量英文 → 标 [机器未译]。"""
    if not title:
        return title
    if not _re.search(r"[A-Za-z]", title):
        return title  # 已是中文
    out = title
    hit = False
    for en, cn in _RSS_PHRASE_CN:
        if en in out:
            out = out.replace(en, cn)
            hit = True
    # 仍残留较多英文单词（>2 个连续字母词）→ 诚实标注未完全翻译，不脑补
    residual = _re.findall(r"[A-Za-z]{3,}", out)
    if residual and len(residual) >= 3:
        return f"{out}　[机器未译·原文]"
    return out


def _translate_poly_market(market: str) -> str:
    """Polymarket 英文事件名 → 中文（模板化覆盖高频句式）。未覆盖 → 标 [机器未译]。"""
    if not market:
        return market
    s = market.strip()
    # Fed rate cuts count: "Will N Fed rate cut(s) happen in YYYY?"
    m = _re.match(r"Will (\d+)(?:\s*or more)? Fed rate cuts? happen in (\d{4})\?", s)
    if m:
        more = "或以上" if "or more" in s else ""
        return f"{m.group(2)}年美联储降息 {m.group(1)} 次{more}?"
    if _re.match(r"Will no Fed rate cuts happen in (\d{4})\?", s):
        yr = _re.search(r"(\d{4})", s).group(1)
        return f"{yr}年美联储不降息?"
    if _re.match(r"Fed rate hike in (\d{4})\?", s):
        yr = _re.search(r"(\d{4})", s).group(1)
        return f"{yr}年美联储加息?"
    # Fed bps after meeting
    m = _re.match(r"Will the Fed (decrease|increase) interest rates by (\d+)\+? bps after the (\w+ \d{4}) meeting\?", s)
    if m:
        direction = "降息" if m.group(1) == "decrease" else "加息"
        meeting = _MONTH_CN(m.group(3))
        plus = "+" if "+" in s else ""
        return f"美联储在{meeting}会议后{direction} {m.group(2)}{plus} 基点?"
    if _re.match(r"Will there be no change in Fed interest rates after the (\w+ \d{4}) meeting\?", s):
        meeting = _MONTH_CN(_re.search(r"after the (\w+ \d{4})", s).group(1))
        return f"美联储在{meeting}会议后维持利率不变?"
    # recession
    m = _re.match(r"US recession by end of (\d{4})\?", s)
    if m:
        return f"{m.group(1)}年底前美国陷入衰退?"
    # inflation > X% in YYYY
    m = _re.match(r"Will inflation reach more than ([\d.]+)% in (\d{4})\?", s)
    if m:
        return f"{m.group(2)}年通胀超过 {m.group(1)}%?"
    # annual inflation be X% in May
    m = _re.match(r"Will annual inflation be ([\d.]+)%( or more)? in (\w+)\?", s)
    if m:
        more = "或以上" if m.group(2) else ""
        return f"{_MONTH_CN(m.group(3))}年化通胀为 {m.group(1)}%{more}?"
    # country annual inflation range
    m = _re.match(r"Will ([\w’']+?)(?:'s|’s)? (\d{4}) Annual Inflation be between ([\d.]+)% and ([\d.]+)%\?", s)
    if m:
        country = _COUNTRY_CN(m.group(1))
        return f"{country}{m.group(2)}年化通胀介于 {m.group(3)}%~{m.group(4)}%?"
    m = _re.match(r"Will ([\w’']+?)(?:'s|’s)? monthly inflation in (\w+ \d{4}) be between ([\d.]+)% and ([\d.]+)\%?\?", s)
    if m:
        country = _COUNTRY_CN(m.group(1))
        return f"{country}{_MONTH_CN(m.group(2))}月度通胀介于 {m.group(3)}%~{m.group(4)}%?"
    # AI: "Will <X> have the best AI model at the end of <Month YYYY>?"
    m = _re.match(r"Will ([\w .&'-]+?) have the best AI model at the end of (\w+ \d{4})\?", s)
    if m:
        return f"{_ORG_CN(m.group(1))}在{_MONTH_CN(m.group(2))}底拥有最强AI模型?"
    # geopolitics: "Will <A> invade <B> (by|before) <when>?"
    m = _re.match(r"Will (?:the )?([\w .'-]+?) invade ([\w .'-]+?) (by|before) (end of )?(\d{4})\?", s)
    if m:
        return f"{_ORG_CN(m.group(1))}在{m.group(5)}年{'底前' if m.group(4) else '前'}入侵{_ORG_CN(m.group(2))}?"
    # crypto: "Will Bitcoin reach $X(k) (in|by) <when>?"
    m = _re.match(r"Will (Bitcoin|Ethereum|BTC|ETH) (?:reach|hit) \$?([\d,]+k?)(?: .*)?\?", s)
    if m:
        coin = {"Bitcoin": "比特币", "BTC": "比特币", "Ethereum": "以太坊", "ETH": "以太坊"}.get(m.group(1), m.group(1))
        return f"{coin}涨至 ${m.group(2)}?"
    # election: "Will <X> win the YYYY US Presidential Election?"
    m = _re.match(r"Will ([\w .'-]+?) win the (\d{4}) US Presidential Election\?", s)
    if m:
        return f"{_ORG_CN(m.group(1))}赢得{m.group(2)}年美国总统大选?"
    # tech_corp: "Will <X> be the largest company in the world by market cap on <when>?"
    m = _re.match(r"Will ([\w .'-]+?) be the largest company in the world by market cap on (\w+ \d+)\?", s)
    if m:
        return f"{_ORG_CN(m.group(1))}在{_MONTH_CN(m.group(2))}成为全球市值最大公司?"
    # fallback: honest untranslated mark
    return f"{s}　[机器未译·原文]"


# 机构/国家专名 → 中文（AI 公司、国家）
_ORG_CN = lambda s: {
    "xAI": "xAI", "Anthropic": "Anthropic", "OpenAI": "OpenAI", "Google": "谷歌",
    "Google DeepMind": "谷歌DeepMind", "Meta": "Meta", "DeepSeek": "DeepSeek",
    "U.S.": "美国", "US": "美国", "United States": "美国", "the U.S.": "美国",
    "China": "中国", "Iran": "伊朗", "Taiwan": "台湾", "Russia": "俄罗斯",
    "Ukraine": "乌克兰", "Israel": "以色列",
    "Tesla": "特斯拉", "Amazon": "亚马逊", "Apple": "苹果", "Microsoft": "微软",
    "Nvidia": "英伟达", "NVIDIA": "英伟达", "Alphabet": "Alphabet(谷歌)",
    "LeBron James": "勒布朗·詹姆斯", "Tim Walz": "蒂姆·沃尔兹",
}.get(s.strip(), s.strip())


_MONTHS = {"January": "1月", "February": "2月", "March": "3月", "April": "4月",
           "May": "5月", "June": "6月", "July": "7月", "August": "8月",
           "September": "9月", "October": "10月", "November": "11月", "December": "12月"}
_COUNTRIES = {"Argentina": "阿根廷", "Mexico": "墨西哥", "India": "印度",
              "Brazil": "巴西", "Turkey": "土耳其", "US": "美国"}


def _MONTH_CN(s: str) -> str:
    for en, cn in _MONTHS.items():
        s = s.replace(en, cn)
    # "6月 2026" → "2026年6月"（月在前年在后时归一为中文语序）
    m = _re.match(r"^(\d{1,2})月\s*(\d{4})$", s.strip())
    if m:
        return f"{m.group(2)}年{m.group(1)}月"
    # "6月 30" → "6月30日"（月+日）
    m = _re.match(r"^(\d{1,2})月\s+(\d{1,2})$", s.strip())
    if m:
        return f"{m.group(1)}月{m.group(2)}日"
    return s


def _COUNTRY_CN(s: str) -> str:
    return _COUNTRIES.get(s.strip("’'"), s)


def _translate_outcome(o: str) -> str:
    return {"Yes": "是", "No": "否"}.get((o or "").strip(), o)


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
                src_name = feed.get("name") or feed.get("source_org") or feed.get("rss_id")
                items.append({
                    "title": _translate_rss_title(it.get("title", "")),
                    "title_en": it.get("title", ""),          # 保留原文便于追溯
                    "link": it.get("link", ""),
                    "published": it.get("published", ""),
                    "source": _RSS_SOURCE_CN.get(src_name, src_name),
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
                    "theme": _POLY_THEME_CN.get(theme, theme),
                    "theme_key": theme,
                    "market": _translate_poly_market(m.get("market", "")),
                    "market_en": m.get("market", ""),          # 保留原文便于追溯
                    "outcome": _translate_outcome(m.get("outcome", "")),
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
                "win_rate": s.get("win_rate"),           # None=样本不足/不参与裁决
                "win_rate_metric": s.get("win_rate_metric", "absolute"),  # 口径标注
                "avg_ret_5d": s.get("avg_ret_5d"),       # 绝对5日收益
                "avg_excess_ret_5d": s.get("avg_excess_ret_5d"),   # 超额5日(跑赢沪深300)
                "avg_excess_ret_20d": s.get("avg_excess_ret_20d"), # 超额20日
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
    """卖水人 Top 环节。只读结构化源 data/dispatch/water_sellers.json（代码经双源核验、
    判定遵循 water-seller-finder SKILL）。无文件/解析失败 → available=False，不硬解析散文报告、不脑补。"""
    src = WORKSPACE_SCRIPTS / "data" / "dispatch" / "water_sellers.json"
    payload = _read_json_safe(src)
    if not isinstance(payload, dict) or not payload.get("items"):
        return {
            "available": False,
            "note": "暂无结构化卖水人数据源（water_sellers.json 未就绪）",
            "items": [],
        }
    items = []
    for it in payload.get("items", []):
        if not isinstance(it, dict):
            continue
        tickers = [{"code": (t.get("code") or "").strip(),
                    "name": (t.get("name") or "").strip(),
                    "role": t.get("role")}
                   for t in (it.get("tickers") or []) if isinstance(t, dict)
                   and (t.get("code") or t.get("name"))]
        if not tickers:
            continue
        items.append({
            "segment": it.get("segment", ""),
            "chain": it.get("chain"),
            "tickers": tickers,
            "logic": it.get("logic", ""),
            "evidence_grade": it.get("evidence_grade"),
        })
    return {
        "available": bool(items),
        "note": payload.get("note") or "",
        "as_of": payload.get("as_of"),
        "items": items,
    }


# ───────────────── 6) 持仓清单（公开安全：只给代码+名称，绝不给数量/成本/盈亏） ─────────────────
# 🔴 铁律：HOLDINGS.md 含持仓量/成本/盈亏，本脚本【只取代码与名称】，绝不输出任何数量、成本、盈亏字段。
#   _parse_holdings 是唯一解析入口；下游 build_held_etf_codes / build_held_stocks 都只取 code/name/type。

# HOLDINGS.md 各持仓小节标题 → 看板分类（type）。仅「持仓」性质小节计入自选展示。
#   关注/候选/已平仓不算「持有」，但「AI产业链关注」是关注池不是持仓，故排除。
_HOLDING_SECTIONS = {
    "A股持仓": "a",
    "ETF持仓": "etf",
    "港股持仓": "hk",
}


def _parse_holdings() -> list[dict]:
    """解析 HOLDINGS.md 各持仓小节 → [{code,name,type}]（公开安全，绝不含量/成本/盈亏）。

    只读「A股持仓 / ETF持仓 / 港股持仓」三类持仓小节；
    「AI产业链关注 / 候选池 / 已平仓记录」等非持仓小节一律跳过。
    找不到文件或解析失败 → 返回空列表（前端据此优雅降级）。
    """
    import re
    if not HOLDINGS_MD.is_file():
        return []
    try:
        text = HOLDINGS_MD.read_text(encoding="utf-8")
    except Exception:
        return []
    out: list[dict] = []
    seen: set = set()
    cur_type: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            title = stripped.lstrip("#").strip()
            cur_type = None
            for sec, t in _HOLDING_SECTIONS.items():
                if sec in title:
                    cur_type = t
                    break
            continue
        if cur_type is None or not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        code, name = cells[0], cells[1]
        # 表头/分隔行（代码 / --- / 空）跳过；A股·ETF=6位数字，港股=5位数字(如 09988)
        if not re.fullmatch(r"\d{5,6}", code):
            continue
        if not name or name in ("名称", "-"):
            continue
        key = (code, cur_type)
        if key in seen:
            continue
        seen.add(key)
        # 🔴 仅取 code/name/type —— 绝不读取后续 持仓日期/持仓价/备注 等敏感列
        out.append({"code": code, "name": name, "type": cur_type})
    return out


def build_held_etf_codes() -> list[str]:
    """持仓 ETF 代码列表（供 H5 把持仓 ETF 排前）。只取代码，绝不含数量/成本/盈亏。"""
    return [h["code"] for h in _parse_holdings() if h["type"] == "etf"]


def build_held_stocks() -> dict:
    """持仓股清单（A股+ETF+港股），供 ⭐自选 tab 展示「实际持仓股」。

    🔴 公开看板铁律：只输出 代码 + 名称 + 市场类型，标记「持有」；
       绝不输出任何持仓量 / 成本 / 盈亏。无文件 → available=False / 空列表。
    """
    items = _parse_holdings()
    return {
        "available": bool(items),
        "note": "" if items else "暂无持仓清单（HOLDINGS.md 未就绪）",
        # held=True 仅作前端标「持有」用，不含任何数量
        "items": [{"code": h["code"], "name": h["name"], "type": h["type"], "held": True}
                  for h in items],
    }


# ───────────── 7) 蛋糕层映射 / 个股分析卡 / 美股→A股映射索引（纯结构化派生） ─────────────
# 下列函数都【只读已构建好的 chains/usMapping/heldStocks 区块】做结构化派生，
#   不再发起任何引擎/网络调用 → 离线、向后兼容、绝不脑补、绝不含持仓量。

# tier → 五层蛋糕层（公开看板只用 L1–L5 概念，按产业链 tier 归层）。
#   tier0 整机/终端→L5 应用；tier1 核心部件→L4；tier2 关键器件→L3；tier3 底层材料/设备→L2；其它→L1。
_TIER_TO_CAKE = {0: "L5", 1: "L4", 2: "L3", 3: "L2"}
_CAKE_NAME = {"L1": "L1 能源基建", "L2": "L2 算力芯片", "L3": "L3 通信基建",
              "L4": "L4 模型层", "L5": "L5 应用层"}

# 海外锚中文名兜底（公开看板：item4 英文全译中文；上游 cn_name 残留英文时在此补译）。
_US_NAME_CN = {
    "Arista Networks": "Arista（云网络）", "Vertiv": "维谛技术",
    "Marvell": "迈威尔", "Coherent": "高意", "Credo": "Credo（互联）",
    "Astera Labs": "Astera（互联）", "Pure Storage": "Pure（存储）",
}


def _clean_us_name(name: str) -> str:
    """把海外锚名里的英文括注/纯英文统一为中文（找不到映射则去掉英文括注）。"""
    if not name:
        return name
    for en, cn in _US_NAME_CN.items():
        if en.lower() in name.lower():
            return cn
    # 形如「维谛技术(Vertiv)」→ 去掉英文括注；「超微电脑(SMCI)」保留中文主名
    import re
    m = re.match(r"^([一-龥·]+)\s*[（(][A-Za-z0-9 .]+[)）]\s*$", name)
    if m:
        return m.group(1)
    return name


def _build_code_segment_index(chains: dict) -> dict:
    """code → {chain,label,segment,tier,is_leader,consensus_state} 首次命中（含 ai_server 优先）。"""
    idx: dict = {}
    # ai_server 优先，保证 AI 链命中覆盖其它链
    order = ["ai_server"] + [k for k in chains.keys() if k != "ai_server"]
    for ck in order:
        c = chains.get(ck) or {}
        for s in c.get("segments", []) or []:
            for h in s.get("holdings", []) or []:
                code = h.get("code")
                if not code or code in idx:
                    continue
                idx[code] = {
                    "chain": ck,
                    "chain_label": c.get("label", ck),
                    "segment": s.get("name"),
                    "tier": s.get("tier"),
                    "is_leader": bool(h.get("is_leader")),
                    "consensus_state": h.get("consensus_state", "unknown"),
                }
    return idx


def enrich_held_stocks(held: dict, chains: dict) -> dict:
    """item10：给每只持仓股映射 链/环节/tier/蛋糕层。映射不到 → 字段缺省（前端不标层）。
    绝不新增任何持仓量字段。"""
    idx = _build_code_segment_index(chains)
    for h in held.get("items", []) or []:
        meta = idx.get(h["code"])
        if meta:
            h["chain"] = meta["chain"]
            h["chain_label"] = meta["chain_label"]
            h["segment"] = meta["segment"]
            h["tier"] = meta["tier"]
            h["cakeLayer"] = _TIER_TO_CAKE.get(meta["tier"], "L1") if meta["tier"] is not None else None
            h["cakeLayerName"] = _CAKE_NAME.get(h["cakeLayer"]) if h.get("cakeLayer") else None
    return held


def build_us_ash_index(us_mapping: dict, chain: str, dry_run: bool) -> dict:
    """item8：把美股锚→A股 映射成 {a_share_code: [{ticker,cn_name,change,big_mover}...]} 索引，
    供前端在对应 A 股标的旁标注「🇺🇸英伟达↑→映射」。

    数据来源：
      - 已大涨锚（big_movers）：其 segments 内全部 A 股标的（big_mover=True，前端高亮）；
      - 全部锚（anchors）：通过其 chain_anchor_segment 经 map_to_ashare 找同环节 A 股标的，
        作为常驻「锚关系」标注（big_mover=False，change 为实际涨跌或 None=未取到）。
    无行情时 change=None（前端标「未取到」，不脑补涨跌）。任何失败 → 该锚跳过。
    """
    out = {"available": False, "note": "未取到", "byCode": {}}
    if not us_mapping or not us_mapping.get("available"):
        out["note"] = us_mapping.get("note", "未取到") if us_mapping else "未取到"
        return out
    by_code: dict = {}

    def _push(code: str, anchor: dict):
        if not code:
            return
        lst = by_code.setdefault(code, [])
        # 同 ticker 已存在：若新条目是大涨锚则升级标记
        for x in lst:
            if x["ticker"] == anchor["ticker"]:
                if anchor.get("big_mover"):
                    x["big_mover"] = True
                    x["change"] = anchor.get("change")
                return
        lst.append(anchor)

    # 1) 大涨锚：直接用其已映射的 A 股链 segments（高亮）
    for m in us_mapping.get("big_movers", []) or []:
        anchor = {"ticker": m.get("ticker"), "cn_name": _clean_us_name(m.get("cn_name")),
                  "change": m.get("change"), "big_mover": True}
        for s in m.get("segments", []) or []:
            for h in s.get("holdings", []) or []:
                _push(h.get("code"), anchor)

    # 2) 全部锚常驻关系：经 chain_anchor_segment → map_to_ashare 找同环节 A 股标的
    anchor_meta = {}
    try:
        if _ensure_workspace_on_path():
            import us_ash_mapping as um  # type: ignore
            anchors_data = um.load_anchors()
            anchor_meta = anchors_data.get("anchors", {})
    except Exception:
        anchor_meta = {}
    # 锚行情（涨跌/大涨标记）从 us_mapping.anchors 取
    chg_by_ticker = {a["ticker"]: a for a in us_mapping.get("anchors", []) or []}
    for ticker, meta in anchor_meta.items():
        seg = meta.get("chain_anchor_segment")
        if not seg:
            continue
        try:
            import us_ash_mapping as um  # type: ignore
            res = um.map_to_ashare(chain, seg, dry_run)
        except Exception:
            res = None
        if not res or res.get("_error"):
            continue
        try:
            flat: list = []
            um.flatten_segments(res["root"], flat)
        except Exception:
            continue
        arow = chg_by_ticker.get(ticker, {})
        anchor = {"ticker": ticker, "cn_name": _clean_us_name(meta.get("cn_name", ticker)),
                  "change": arow.get("change"), "big_mover": bool(arow.get("big_mover")),
                  "segment": seg}
        for s in flat:
            for h in s.get("holdings", []) or []:
                _push(h.get("code"), anchor)

    out["byCode"] = by_code
    out["available"] = bool(by_code)
    out["quote_ok"] = bool(us_mapping.get("quote_ok"))
    out["note"] = "" if by_code else "暂无美股锚→A股映射（图谱待对齐）"
    return out


def build_stock_cards(chains: dict, held: dict, us_index: dict) -> dict:
    """item7：为「系统已覆盖股」预计算只读分析卡，供个股输入仪表盘展示。

    覆盖范围 = 各产业链 segments 上的全部 A 股标的（已挂树即视为已覆盖）。
    每张卡（公开安全，绝不含持仓量/成本/盈亏）：
      code/name · chain/segment/tier/卡点层级(chokepoint) · 共识态 · 是否龙头
      · 卖水人环节判定(seg 是否上游器件/材料=卖水人候选) · 美股映射 · 是否持有
    估值(PE/市值)/技术信号 由前端用 data.json 现有行情字段补齐（feed 不重复取行情）。
    """
    held_codes = {h["code"] for h in (held.get("items") or [])}
    idx = _build_code_segment_index(chains)
    cards: dict = {}
    for code, meta in idx.items():
        tier = meta["tier"]
        cake = _TIER_TO_CAKE.get(tier, "L1") if tier is not None else None
        # 卖水人候选：上游器件/材料/设备（tier>=2）→「卖水人环节」（卖铲子的人）
        water_seller = tier is not None and tier >= 2
        # 卡点层级文案
        choke = {0: "整机集成（下游需求侧）", 1: "核心部件（议价中枢）",
                 2: "关键器件/材料（潜在卡点）", 3: "底层材料/设备（最上游卡点）"}.get(
                     tier, "环节待确认")
        cs = meta["consensus_state"]
        cards[code] = {
            "code": code,
            "name": next((h["name"] for s in (chains.get(meta["chain"]) or {}).get("segments", [])
                          for h in (s.get("holdings") or []) if h.get("code") == code), code),
            "chain": meta["chain"],
            "chain_label": meta["chain_label"],
            "segment": meta["segment"],
            "tier": tier,
            "cakeLayer": cake,
            "cakeLayerName": _CAKE_NAME.get(cake) if cake else None,
            "is_leader": meta["is_leader"],
            "consensus_state": cs,
            "chokepoint": choke,
            "water_seller": water_seller,
            "held": code in held_codes,
            "us_anchors": (us_index.get("byCode") or {}).get(code, []),
            "selectionProfile": _selection_profile(
                tier=tier,
                is_leader=bool(meta["is_leader"]),
                water_seller=water_seller,
                consensus_state=cs,
                held_flag=code in held_codes,
                chokepoint=choke,
            ),
            "timingProfile": _timing_profile(),
        }
    return {
        "available": bool(cards),
        "note": "" if cards else "暂无已覆盖标的（产业链图谱待补）",
        "count": len(cards),
        "cards": cards,
    }


def _selection_profile(*, tier: int | None, is_leader: bool, water_seller: bool,
                       consensus_state: str | None, held_flag: bool, chokepoint: str) -> dict:
    """Structural value-investing profile. Live valuation is added by H5/data.json."""
    score = 45
    reasons: list[str] = []

    if water_seller:
        score += 20
        reasons.append("卖水人/上游供给环节")
    if is_leader:
        score += 15
        reasons.append("环节龙头")

    if tier == 3:
        score += 15
        role = "上游材料设备"
    elif tier == 2:
        score += 12
        role = "关键器件材料"
    elif tier == 1:
        score += 6
        role = "核心系统平台"
    elif tier == 0:
        role = "终端整机需求"
    else:
        role = "产业链位置待确认"

    if consensus_state == "non_consensus":
        score += 10
        reasons.append("非共识环节")
    elif consensus_state == "crowded":
        score -= 15
        reasons.append("共识拥挤")

    if held_flag:
        score += 3
        reasons.append("已纳入持仓跟踪")

    if chokepoint and chokepoint not in reasons:
        reasons.append(chokepoint)

    score = max(0, min(100, score))
    if score >= 78:
        label = "价值优先候选"
    elif score >= 62:
        label = "结构候选"
    elif consensus_state == "crowded":
        label = "拥挤观察"
    else:
        label = "观察"

    return {
        "label": label,
        "score": score,
        "role": role,
        "reasons": reasons[:4],
        "valuation_required": True,
        "valuation_note": "需叠加 PE/市值/ROE/毛利率趋势后再做价值结论",
    }


def _timing_profile() -> dict:
    return {
        "label": "等待实时量化信号",
        "source": "data.json.signal / signalNote",
        "rules": [
            "buy=可等买点",
            "risk=规避/减仓",
            "neutral=等待确认",
            "涨幅过大=追高谨慎",
            "明显回撤=回撤观察",
        ],
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

    print(f"[1/5] 产业链树 / 非共识机会（全部 {len(CHAINS_ALL)} 链）...")
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

    held_etf = build_held_etf_codes()
    print(f"      持仓 ETF 代码（仅代码，无数量）: {len(held_etf)} 个 {held_etf or '—'}")

    held_stocks = build_held_stocks()
    held_stocks = enrich_held_stocks(held_stocks, all_chains["chains"])
    _mapped = sum(1 for h in held_stocks["items"] if h.get("cakeLayer"))
    print(f"      持仓股清单（仅代码+名称，无数量/成本/盈亏）: {len(held_stocks['items'])} 只 · 已映射蛋糕层 {_mapped} 只")

    print("[派生] 美股→A股映射索引 + 个股分析卡（结构化派生，不再取数）...")
    us_index = build_us_ash_index(us_mapping, args.chain, args.dry_run)
    stock_cards = build_stock_cards(all_chains["chains"], held_stocks, us_index)
    print(f"      美股→A股映射索引: {'OK' if us_index['available'] else 'SKIP'} · "
          f"命中 A 股 {len(us_index.get('byCode', {}))} 只 · {us_index.get('note') or '—'}")
    print(f"      个股分析卡（已覆盖股预计算）: {'OK' if stock_cards['available'] else 'SKIP'} · "
          f"{stock_cards.get('count', 0)} 张 · {stock_cards.get('note') or '—'}")

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
        "heldEtfCodes": held_etf,           # 持仓 ETF 代码（公开安全：仅代码，前端据此排序，不含数量）
        "heldStocks": held_stocks,          # 持仓股清单（公开安全：仅代码+名称+蛋糕层映射，绝不含数量/成本/盈亏）
        "usAshIndex": us_index,             # item8：美股锚→A股 映射索引（byCode），UI 在 A 股标的旁标注
        "stockCards": stock_cards,          # item7：已覆盖股的预计算只读分析卡（个股仪表盘用）
    }

    # 原子写: 写临时文件再 rename, 防中断写出半截 JSON 致 H5 白屏
    import os as _os
    _tmp = OUTPUT_FILE.with_name(OUTPUT_FILE.name + ".tmp")
    _tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _os.replace(_tmp, OUTPUT_FILE)
    print()
    print(f"✅ 已写入: {OUTPUT_FILE}")
    available = [k for k in ("waterSellers", "chainTree", "usMapping", "funding", "winRate", "intel")
                 if out[k].get("available")]
    print(f"   可用区块: {', '.join(available) if available else '（全部降级，无可用区块）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
