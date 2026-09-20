#!/usr/bin/env python3
"""
扶摇（同花顺）数据源 — invest-cake 行情第一优先级
================================================
Base: https://fuyao.aicubes.cn   鉴权: header X-api-key
Key 只从环境变量 FUYAO_API_KEY 读取（本机在 .env.local 配置，不入库）。

能力边界（2026-09-21 实测）：
  ✅ A股实时快照   /api/a-share/prices/snapshot        批量（逗号分隔）
  ✅ A股估值快照   /api/a-share/valuations/snapshot    批量，pe_ttm/pb_mrq/ps_ttm
  ✅ 指数快照      /api/a-share-index/prices/snapshot  批量（000001.SH...）
  ✅ 基金/ETF快照  /api/fund/market/snapshot           单只（thscode 逐个请求）
  ❌ 港股          a-share 接口不认 HK 代码 → 港股继续走新浪/富途
  ❌ 总市值        valuations 无市值字段 → cap 继续走 AKShare 兜底

所有函数失败返回空 dict，绝不抛异常，保证上层可降级到原新浪/AKShare 链路。
"""

import os
from pathlib import Path
import requests

BASE = "https://fuyao.aicubes.cn"
TIMEOUT = 15


def _key() -> str:
    """优先环境变量，其次仓库内 .env.local（git 已忽略）。拿不到就返回空 → 上层走兜底源。"""
    k = os.environ.get("FUYAO_API_KEY", "").strip()
    if k:
        return k
    try:
        env_file = Path(__file__).resolve().parent / ".env.local"
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("FUYAO_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def available() -> bool:
    return bool(_key())


def _get(path: str, **params):
    """返回 list[item]，任何异常/业务错误都返回 []"""
    key = _key()
    if not key:
        return []
    try:
        r = requests.get(
            BASE + path,
            headers={"X-api-key": key, "Accept": "application/json"},
            params=params,
            timeout=TIMEOUT,
        )
        payload = r.json()
    except Exception as e:
        print(f"  [WARN] 扶摇 {path} 请求失败: {e}")
        return []
    # HTTP 200 也可能是业务错误，必须判 code
    if payload.get("code") != 0:
        print(f"  [WARN] 扶摇 {path} 业务错误: code={payload.get('code')} {payload.get('message')}")
        return []
    data = payload.get("data") or {}
    return data.get("item") or []


def to_thscode(code: str) -> str:
    """6 位代码 → 扶摇 thscode。6/5 开头沪市，其余深市。"""
    code = code.strip()
    if "." in code:
        return code
    if code.startswith(("6", "5")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _fmt(item: dict, pe="-", cap="-") -> dict:
    """扶摇 item → 与新浪 fetch_sina_quotes 一致的行情结构"""
    try:
        price = float(item.get("last_price") or 0)
        cv = float(item.get("price_change_ratio_pct") or 0)
    except (TypeError, ValueError):
        return {}
    if price <= 0:
        return {}
    return {
        "price": str(round(price, 3)),
        "chg": f"+{round(cv, 2)}%" if cv > 0 else f"{round(cv, 2)}%",
        "cv": round(cv, 2),
        "pe": pe,
        "cap": cap,
    }


def is_etf(code: str) -> bool:
    """ETF/LOF 代码识别：5xx/1xx 开头走基金接口，其余(6/0/3)走A股接口"""
    return code.strip().startswith(("5", "1"))


def fetch_quotes(codes: list, with_pe: bool = True) -> dict:
    """统一入口：自动分流 A股批量 + ETF 逐个 → {code: {...}}"""
    codes = [c for c in (codes or []) if c and not c.startswith("HK.")]
    a_codes = [c for c in codes if not is_etf(c)]
    e_codes = [c for c in codes if is_etf(c)]

    out = {}
    if a_codes:
        out.update(fetch_stocks(a_codes, with_pe=with_pe))
    if e_codes:
        out.update(fetch_etfs(e_codes))
    return out


def fetch_stocks(codes: list, with_pe: bool = True) -> dict:
    """A股实时快照（批量；单个坏码会整批失败 → 自动降级逐只重试）"""
    codes = [c for c in codes if c and not c.startswith("HK.") and not is_etf(c)]
    if not codes:
        return {}

    items = _get("/api/a-share/prices/snapshot", thscodes=",".join(to_thscode(c) for c in codes))
    if not items and len(codes) > 1:
        # 批量里只要有一个未知代码就整体 1002，退化为逐只请求
        for c in codes:
            items += _get("/api/a-share/prices/snapshot", thscodes=to_thscode(c))
    if not items:
        return {}

    pe_map = fetch_valuations(codes) if with_pe else {}
    out = {}
    for it in items:
        code = str(it.get("ticker") or "").replace(".SH", "").replace(".SZ", "")
        if not code:
            continue
        q = _fmt(it, pe=pe_map.get(code, "-"))
        if q:
            out[code] = q
    return out


def fetch_valuations(codes: list) -> dict:
    """A股估值快照 → {code: pe_ttm(保留2位)}。失败返回 {}"""
    codes = [c for c in codes if c and not c.startswith("HK.") and not is_etf(c)]
    if not codes:
        return {}
    items = _get("/api/a-share/valuations/snapshot", thscodes=",".join(to_thscode(c) for c in codes))
    out = {}
    for it in items:
        code = str(it.get("ticker") or "").replace(".SH", "").replace(".SZ", "")
        pe = it.get("pe_ttm")
        if code and pe not in (None, ""):
            try:
                out[code] = round(float(pe), 2)
            except (TypeError, ValueError):
                pass
    return out


def fetch_etfs(codes: list) -> dict:
    """ETF/基金快照（接口只支持单只，逐个请求）→ {code: {...}}"""
    out = {}
    for code in codes:
        if not code or code.startswith("HK."):
            continue
        items = _get("/api/fund/market/snapshot", thscode=to_thscode(code))
        for it in items:
            c = str(it.get("ticker") or "").replace(".SH", "").replace(".SZ", "") or code
            q = _fmt(it)
            if q:
                out[c] = q
    return out


def _fmt_cap(value: float) -> str:
    """市值格式化：与 AKShare 口径一致（万亿/亿/元）"""
    if value >= 1e12:
        return f"{round(value / 1e12, 2)}万亿"
    if value >= 1e8:
        return f"{round(value / 1e8, 2)}亿"
    return str(round(value))


def fetch_market_caps(codes: list) -> dict:
    """流通市值（元→亿/万亿字符串）→ {code: cap_str}
    来源 `/api/a-share/auction/snapshot` 的 float_market_cap。
    ⚠️ 是**流通市值**，不是总市值；ETF/港股不支持。
    """
    codes = [c for c in (codes or []) if c and not c.startswith("HK.") and not is_etf(c)]
    if not codes:
        return {}

    items = _get("/api/a-share/auction/snapshot",
                 thscodes=",".join(to_thscode(c) for c in codes), stage="final")
    if not items and len(codes) > 1:
        for c in codes:
            items += _get("/api/a-share/auction/snapshot", thscodes=to_thscode(c), stage="final")

    out = {}
    for it in items:
        code = str(it.get("ticker") or "").replace(".SH", "").replace(".SZ", "")
        v = it.get("float_market_cap")
        if not code or v in (None, ""):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            out[code] = _fmt_cap(fv)
    return out


def fetch_history(code: str, days: int = 200) -> dict:
    """日线历史（单只，前复权）→ {close,high,low,volume,amount,date}，不足 30 根返回 {}
    A股走 /api/a-share/prices/historical，ETF/LOF 走 /api/fund/market/historical。
    港股不支持 → 返回 {}
    """
    if not code or code.startswith("HK."):
        return {}
    import time

    etf = is_etf(code)
    path = "/api/fund/market/historical" if etf else "/api/a-share/prices/historical"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * 3600 * 1000)
    params = {"thscode": to_thscode(code), "interval": "1d", "start": start_ms, "end": end_ms}
    if not etf:
        params["adjust"] = "forward"

    items = _get(path, **params)
    if not items:
        return {}
    items = sorted(items, key=lambda x: x.get("date_ms") or 0)

    bars = {"close": [], "high": [], "low": [], "open": [], "volume": [], "amount": [], "date": []}
    for it in items:
        try:
            c = float(it.get("close_price"))
            h = float(it.get("high_price"))
            lo = float(it.get("low_price"))
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        bars["close"].append(c)
        bars["high"].append(h)
        bars["low"].append(lo)
        bars["open"].append(float(it.get("open_price") or 0))
        bars["volume"].append(float(it.get("volume") or 0))
        bars["amount"].append(float(it.get("turnover") or 0))
        bars["date"].append(it.get("date_ms"))
    if len(bars["close"]) < 30:
        return {}
    return bars


def fetch_index(code: str = "000001") -> dict:
    """大盘指数快照 → 单条行情 dict 或 {}"""
    ths = f"{code}.SH" if code.startswith("000") else to_thscode(code)
    items = _get("/api/a-share-index/prices/snapshot", thscodes=ths)
    for it in items:
        q = _fmt(it)
        if q:
            return q
    return {}
