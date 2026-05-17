#!/usr/bin/env python3
"""
OpenClaw 六层数据源模块
======================
按照数据架构提供统一接口，每层有主数据源+备用源:

  L1 行情层: 新浪(主) → mootdx(备) → 腾讯(备)
  L2 研报层: 东方财富(AKShare) + iwencai
  L3 信号层: 北向资金 / 龙虎榜 / 解禁 / 行业轮动 (AKShare + 同花顺)
  L4 新闻层: AKShare财经新闻 + 公告摘要
  L5 基础数据: AKShare(主) → mootdx(备) — 财务数据/F10
  L6 公告层: 巨潮资讯网

所有函数失败返回空结构，不抛异常，供上层按需调用。
"""

import os
import json
from datetime import datetime, timedelta

# 绕过代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests as _requests
_orig_init = _requests.Session.__init__
def _patched_init(self, *a, **kw):
    _orig_init(self, *a, **kw)
    self.trust_env = False
    self.proxies = {"http": None, "https": None}
_requests.Session.__init__ = _patched_init
import requests

NOW = datetime.now()


# ================================================================
# L1 行情层: mootdx + 腾讯 (备用源)
# ================================================================

def quote_mootdx(codes):
    """
    mootdx 实时行情 (通达信协议)
    codes: ["600519", "000001", ...]
    返回: {code: {price, chg, chg_pct, volume, amount, high, low, open}}
    需要: pip install mootdx
    """
    results = {}
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market="std")
        for code in codes:
            try:
                # 判断市场: 6开头=沪, 0/3开头=深
                market = 1 if code.startswith("6") else 0
                data = client.quotes(symbol=code, market=market)
                if data is not None and len(data) > 0:
                    row = data.iloc[0]
                    last_close = float(row.get("last_close", 0))
                    price = float(row.get("price", 0))
                    chg = round(price - last_close, 2) if last_close else 0
                    chg_pct = round(chg / last_close * 100, 2) if last_close else 0
                    results[code] = {
                        "price": price,
                        "chg": chg,
                        "chg_pct": chg_pct,
                        "volume": int(row.get("vol", 0)),
                        "amount": float(row.get("amount", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "open": float(row.get("open", 0)),
                    }
            except Exception as e:
                print(f"[mootdx] {code} 失败: {e}")
    except ImportError:
        print("[mootdx] 未安装，跳过 (pip install mootdx)")
    except Exception as e:
        print(f"[mootdx] 连接失败: {e}")
    return results


def quote_tencent(codes):
    """
    腾讯行情HTTP接口 (无需认证)
    codes: ["600519", "000001", ...]
    返回: {code: {price, chg_pct, volume, amount, name}}
    """
    results = {}
    if not codes:
        return results
    try:
        # 腾讯格式: sh600519, sz000001
        tc_codes = []
        for c in codes:
            if c.startswith("6"):
                tc_codes.append(f"sh{c}")
            elif c.startswith("0") or c.startswith("3"):
                tc_codes.append(f"sz{c}")
            elif c.startswith("1") or c.startswith("5"):
                tc_codes.append(f"sz{c}")  # ETF
            else:
                tc_codes.append(f"sh{c}")

        url = f"http://qt.gtimg.cn/q={','.join(tc_codes)}"
        resp = requests.get(url, timeout=8)
        resp.encoding = "gbk"

        for line in resp.text.strip().split("\n"):
            if "=" not in line or "~" not in line:
                continue
            var_part, data_part = line.split("=", 1)
            fields = data_part.strip('";\n').split("~")
            if len(fields) < 45:
                continue
            try:
                code = fields[2]
                results[code] = {
                    "name": fields[1],
                    "price": float(fields[3]) if fields[3] else 0,
                    "chg_pct": float(fields[32]) if fields[32] else 0,
                    "volume": int(float(fields[6])) if fields[6] else 0,
                    "amount": float(fields[37]) * 10000 if fields[37] else 0,
                    "high": float(fields[33]) if fields[33] else 0,
                    "low": float(fields[34]) if fields[34] else 0,
                    "open": float(fields[5]) if fields[5] else 0,
                    "turnover": float(fields[38]) if fields[38] else 0,  # 换手率
                    "pe": float(fields[39]) if fields[39] else 0,
                    "market_cap": float(fields[45]) if len(fields) > 45 and fields[45] else 0,
                }
            except (ValueError, IndexError):
                pass
    except Exception as e:
        print(f"[腾讯行情] 请求失败: {e}")
    return results


def quote_mootdx_kline(code, days=120):
    """
    mootdx 日K线 (备用于AKShare)
    返回: [{date, open, high, low, close, volume, amount}, ...]
    """
    results = []
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market="std")
        market = 1 if code.startswith("6") else 0
        data = client.bars(symbol=code, market=market, category=9, offset=days)
        if data is not None and len(data) > 0:
            for _, row in data.iterrows():
                results.append({
                    "date": str(row.get("datetime", ""))[:10],
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": int(row.get("vol", 0)),
                    "amount": float(row.get("amount", 0)),
                })
    except ImportError:
        pass
    except Exception as e:
        print(f"[mootdx K线] {code} 失败: {e}")
    return results


# ================================================================
# L2 研报层: 东方财富 + iwencai
# ================================================================

def fetch_research_reports(code=None, count=10):
    """
    东方财富个股研报 (通过AKShare)
    code: 股票代码(如"600519")，None则获取最新研报
    返回: [{title, institution, analyst, rating, date, url}, ...]
    """
    results = []
    try:
        import akshare as ak
        if code:
            df = ak.stock_research_report_em(symbol=code)
        else:
            df = ak.stock_research_report_em()

        if df is not None and len(df) > 0:
            for _, row in df.head(count).iterrows():
                results.append({
                    "title": str(row.get("报告名称", "")),
                    "institution": str(row.get("机构", "")),
                    "analyst": str(row.get("分析师", "")),
                    "rating": str(row.get("评级", "")),
                    "date": str(row.get("日期", ""))[:10],
                })
    except Exception as e:
        print(f"[研报] 获取失败: {e}")
    return results


def fetch_industry_research(industry="", count=10):
    """
    行业研报 (东方财富)
    返回: [{title, institution, date}, ...]
    """
    results = []
    try:
        import akshare as ak
        df = ak.stock_industry_research_report_em()
        if df is not None and len(df) > 0:
            if industry:
                df = df[df.apply(lambda r: industry in str(r), axis=1)]
            for _, row in df.head(count).iterrows():
                results.append({
                    "title": str(row.get("报告名称", row.get("title", ""))),
                    "institution": str(row.get("机构", "")),
                    "date": str(row.get("日期", ""))[:10],
                })
    except Exception as e:
        print(f"[行业研报] 获取失败: {e}")
    return results


# ================================================================
# L3 信号层: 北向资金 / 龙虎榜 / 解禁 / 行业轮动
# ================================================================

def fetch_northbound_flow():
    """
    北向资金(沪股通+深股通)净流入
    返回: {date, net_flow_sh, net_flow_sz, net_flow_total, trend}
    trend: "流入" / "流出" / "平衡"
    """
    result = {"ok": False}
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is not None and len(df) >= 2:
            latest = df.iloc[-1]
            prev = df.iloc[-2]

            # 字段名可能因AKShare版本不同
            cols = df.columns.tolist()
            val_col = [c for c in cols if "净流入" in c or "净买入" in c or "合计" in c]

            if val_col:
                net_total = float(latest[val_col[0]])
                net_prev = float(prev[val_col[0]])
            else:
                # 尝试按位置取
                net_total = float(latest.iloc[1]) if len(latest) > 1 else 0
                net_prev = float(prev.iloc[1]) if len(prev) > 1 else 0

            result = {
                "ok": True,
                "date": str(latest.iloc[0])[:10] if len(latest) > 0 else "",
                "net_flow": round(net_total / 1e8, 2),  # 亿元
                "net_flow_prev": round(net_prev / 1e8, 2),
                "trend": "流入" if net_total > 1e8 else ("流出" if net_total < -1e8 else "平衡"),
                "consecutive": 0,  # 连续天数，正=流入，负=流出
            }

            # 统计连续流入/流出天数
            streak = 0
            direction = 1 if net_total > 0 else -1
            for i in range(len(df) - 1, max(len(df) - 30, -1), -1):
                try:
                    v = float(df.iloc[i][val_col[0]]) if val_col else float(df.iloc[i].iloc[1])
                    if (v > 0 and direction > 0) or (v < 0 and direction < 0):
                        streak += 1
                    else:
                        break
                except:
                    break
            result["consecutive"] = streak * direction

    except Exception as e:
        print(f"[北向资金] 获取失败: {e}")
    return result


def fetch_northbound_stock_holding(code):
    """
    北向资金个股持仓变化
    返回: {ok, hold_pct, hold_chg, trend}
    """
    result = {"ok": False}
    try:
        import akshare as ak
        df = ak.stock_hsgt_individual_em(symbol=code)
        if df is not None and len(df) >= 2:
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            cols = df.columns.tolist()
            pct_col = [c for c in cols if "持股比" in c or "占比" in c]
            if pct_col:
                hold_pct = float(latest[pct_col[0]])
                hold_prev = float(prev[pct_col[0]])
                result = {
                    "ok": True,
                    "hold_pct": round(hold_pct, 2),
                    "hold_chg": round(hold_pct - hold_prev, 3),
                    "trend": "增持" if hold_pct > hold_prev else ("减持" if hold_pct < hold_prev else "持平"),
                }
    except Exception as e:
        print(f"[北向持仓] {code} 获取失败: {e}")
    return result


def fetch_dragon_tiger(date=None):
    """
    龙虎榜数据 (当日或指定日期)
    返回: [{code, name, chg_pct, net_buy, reason}, ...]
    """
    results = []
    try:
        import akshare as ak
        if date is None:
            date = NOW.strftime("%Y%m%d")
        df = ak.stock_lhb_detail_em(start_date=date, end_date=date)
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                results.append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "chg_pct": float(row.get("涨跌幅", 0)),
                    "net_buy": float(row.get("净买额", 0)),
                    "reason": str(row.get("上榜原因", "")),
                })
    except Exception as e:
        print(f"[龙虎榜] 获取失败: {e}")
    return results


def fetch_unlock_schedule(days_ahead=30):
    """
    限售股解禁日程 (未来N天)
    返回: [{code, name, date, unlock_shares, unlock_ratio, unlock_value}, ...]
    """
    results = []
    try:
        import akshare as ak
        df = ak.stock_restricted_release_summary_em()
        if df is not None and len(df) > 0:
            end_date = (NOW + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
            today = NOW.strftime("%Y-%m-%d")
            for _, row in df.iterrows():
                try:
                    d = str(row.get("解禁日期", row.get("解除限售日期", "")))[:10]
                    if today <= d <= end_date:
                        results.append({
                            "code": str(row.get("股票代码", row.get("代码", ""))),
                            "name": str(row.get("股票简称", row.get("名称", ""))),
                            "date": d,
                            "unlock_shares": float(row.get("解禁数量", 0)),
                            "unlock_ratio": float(row.get("占总股本比例", row.get("占比", 0))),
                        })
                except:
                    pass
    except Exception as e:
        print(f"[解禁] 获取失败: {e}")
    return results


def check_stock_unlock(code, days_ahead=30):
    """
    检查指定个股未来N天是否有解禁
    返回: {has_unlock, date, ratio, risk_level}
    """
    unlocks = fetch_unlock_schedule(days_ahead)
    for u in unlocks:
        if u["code"] == code:
            ratio = u.get("unlock_ratio", 0)
            risk = "高" if ratio > 5 else ("中" if ratio > 1 else "低")
            return {
                "has_unlock": True,
                "date": u["date"],
                "ratio": ratio,
                "risk_level": risk,
            }
    return {"has_unlock": False}


def fetch_sector_rotation():
    """
    行业轮动 — 同花顺行业板块涨跌排行
    返回: [{name, chg_pct, rank, volume_ratio}, ...]
    """
    results = []
    try:
        import akshare as ak
        df = ak.stock_board_industry_index_ths()
        if df is not None and len(df) > 0:
            # 按涨跌幅排序
            chg_col = [c for c in df.columns if "涨跌" in c]
            if chg_col:
                df = df.sort_values(chg_col[0], ascending=False)

            for rank, (_, row) in enumerate(df.iterrows(), 1):
                name = str(row.get("板块名称", row.get("名称", "")))
                chg = float(row.get(chg_col[0], 0)) if chg_col else 0
                results.append({
                    "name": name,
                    "chg_pct": round(chg, 2),
                    "rank": rank,
                })
    except Exception as e:
        print(f"[行业轮动] 获取失败: {e}")
    return results


def fetch_hot_topics():
    """
    热点题材/概念板块 (同花顺)
    返回: [{name, chg_pct, rank}, ...]
    """
    results = []
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_ths()
        if df is not None and len(df) > 0:
            chg_col = [c for c in df.columns if "涨跌" in c or "涨幅" in c]
            if chg_col:
                df = df.sort_values(chg_col[0], ascending=False)
            for rank, (_, row) in enumerate(df.head(20).iterrows(), 1):
                results.append({
                    "name": str(row.get("板块名称", row.get("概念名称", ""))),
                    "chg_pct": round(float(row.get(chg_col[0], 0)), 2) if chg_col else 0,
                    "rank": rank,
                })
    except Exception as e:
        print(f"[热点题材] 获取失败: {e}")
    return results


# ================================================================
# L4 新闻层: AKShare 财经新闻 + 公告摘要
# ================================================================

def fetch_financial_news(count=20):
    """
    财经新闻 (AKShare → 东方财富)
    返回: [{title, url, time, source}, ...]
    """
    results = []
    try:
        import akshare as ak
        df = ak.stock_news_em()
        if df is not None and len(df) > 0:
            for _, row in df.head(count).iterrows():
                results.append({
                    "title": str(row.get("新闻标题", row.get("title", ""))),
                    "url": str(row.get("新闻链接", row.get("url", ""))),
                    "time": str(row.get("发布时间", ""))[:16],
                    "source": str(row.get("文章来源", "")),
                })
    except Exception as e:
        print(f"[财经新闻] 获取失败: {e}")
    return results


def fetch_stock_news(code, count=10):
    """
    个股新闻 (AKShare)
    返回: [{title, url, time}, ...]
    """
    results = []
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol=code)
        if df is not None and len(df) > 0:
            for _, row in df.head(count).iterrows():
                results.append({
                    "title": str(row.get("新闻标题", "")),
                    "url": str(row.get("新闻链接", "")),
                    "time": str(row.get("发布时间", ""))[:16],
                })
    except Exception as e:
        print(f"[个股新闻] {code} 获取失败: {e}")
    return results


# ================================================================
# L5 基础数据: 财务数据 / F10 (AKShare主 + mootdx备)
# ================================================================

def fetch_financial_data(code):
    """
    财务数据 (AKShare)
    返回: {pe, pb, roe, revenue, profit, profit_chg, debt_ratio, ...}
    """
    result = {"ok": False}
    try:
        import akshare as ak
        # 个股信息
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and len(df) > 0:
            info = {}
            for _, row in df.iterrows():
                key = str(row.iloc[0])
                val = row.iloc[1]
                info[key] = val

            result = {
                "ok": True,
                "total_cap": info.get("总市值", 0),
                "float_cap": info.get("流通市值", 0),
                "pe": info.get("市盈率(动态)", 0),
                "pb": info.get("市净率", 0),
                "industry": info.get("行业", ""),
            }
    except Exception as e:
        print(f"[财务数据] {code} 获取失败: {e}")
    return result


def fetch_f10_mootdx(code):
    """
    F10资料 (mootdx备用)
    返回: {name, industry, main_business, ...}
    """
    result = {"ok": False}
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market="std")
        market = 1 if code.startswith("6") else 0
        data = client.F10(symbol=code, market=market)
        if data:
            result = {"ok": True, "data": data}
    except ImportError:
        pass
    except Exception as e:
        print(f"[mootdx F10] {code} 获取失败: {e}")
    return result


# ================================================================
# L6 公告层: 巨潮资讯网
# ================================================================

def fetch_cninfo_announcements(code=None, count=10):
    """
    巨潮资讯网公告 (通过AKShare)
    code: 股票代码，None则获取最新公告
    返回: [{title, date, url, type}, ...]
    """
    results = []
    try:
        import akshare as ak
        if code:
            df = ak.stock_notice_report(symbol=code)
        else:
            df = ak.stock_notice_report()

        if df is not None and len(df) > 0:
            for _, row in df.head(count).iterrows():
                results.append({
                    "title": str(row.get("公告标题", row.get("标题", ""))),
                    "date": str(row.get("公告日期", row.get("日期", "")))[:10],
                    "url": str(row.get("公告链接", row.get("链接", ""))),
                    "type": str(row.get("公告类型", "")),
                })
    except Exception as e:
        print(f"[巨潮公告] 获取失败: {e}")
    return results


# ================================================================
# 聚合接口: 信号增强数据包
# ================================================================

def fetch_signal_enhancement(code):
    """
    为指定个股获取信号增强数据包
    用于 generate-signals.py 的附加因子

    返回: {
        northbound: {ok, trend, hold_chg},
        unlock: {has_unlock, date, ratio, risk_level},
        on_dragon_tiger: bool,
        sector_rank: int or None,
    }
    """
    enhancement = {
        "northbound": {"ok": False},
        "unlock": {"has_unlock": False},
        "on_dragon_tiger": False,
        "sector_rank": None,
    }

    # 1. 北向资金持仓变化
    try:
        nb = fetch_northbound_stock_holding(code)
        if nb.get("ok"):
            enhancement["northbound"] = nb
    except:
        pass

    # 2. 解禁检查(未来30天)
    try:
        unlock = check_stock_unlock(code, 30)
        enhancement["unlock"] = unlock
    except:
        pass

    # 3. 龙虎榜检查(今日)
    try:
        lhb = fetch_dragon_tiger()
        for item in lhb:
            if item.get("code") == code:
                enhancement["on_dragon_tiger"] = True
                enhancement["dragon_tiger_detail"] = item
                break
    except:
        pass

    return enhancement


# ================================================================
# CLI 测试
# ================================================================
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("OpenClaw 六层数据源 — 可用性测试")
    print("=" * 60)

    # L1 行情
    print("\n[L1 行情] 腾讯行情...")
    qt = quote_tencent(["600519", "000001"])
    for c, d in qt.items():
        print(f"  {c} {d.get('name','')}: {d.get('price',0)} ({d.get('chg_pct',0)}%)")
    print(f"  结果: {'OK' if qt else 'FAIL'}")

    # L3 信号
    print("\n[L3 信号] 北向资金...")
    nb = fetch_northbound_flow()
    print(f"  {nb}")

    print("\n[L3 信号] 行业轮动 TOP5...")
    sectors = fetch_sector_rotation()
    for s in sectors[:5]:
        print(f"  {s['rank']}. {s['name']} {s['chg_pct']}%")

    print("\n[L3 信号] 热点题材 TOP5...")
    topics = fetch_hot_topics()
    for t in topics[:5]:
        print(f"  {t['rank']}. {t['name']} {t['chg_pct']}%")

    # L4 新闻
    print("\n[L4 新闻] 最新财经新闻...")
    news = fetch_financial_news(5)
    for n in news:
        print(f"  [{n['time']}] {n['title'][:40]}")

    print("\n" + "=" * 60)
    print("测试完成")
