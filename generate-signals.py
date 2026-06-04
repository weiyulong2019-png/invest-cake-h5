#!/usr/bin/env python3
"""
OpenClaw 信号生成器 v2 — 乘法因子模型
====================================
升级自v1(加法模型)，核心改进:
  1. 硬排除规则: ST/次新/低流动性 → 直接标记为风险
  2. 乘法因子组合: 单因子极端值可直接压制总分
  3. ADX趋势过滤: ADX<22时信号降级为中性
  4. 信号置信度: 高/中/低(基于因子一致性)
  5. 预期波动提示: 基于ATR的日内波动预估
  6. ETF独立信号: 不再默认中性

每日调度:
  06:00 — 盘前信号（美股隔夜 + 公告/新闻评估）
  09:25 — 集合竞价信号
  整点/半点 — 盘中更新
  15:15/16:15 — 收盘最终信号

数据来源: AKShare(历史K线) / 新浪HTTP(美股指数备选)
输出: data.json → signal/signalNote/signalTime/confidence + layer.summary/summaryTime
"""

import os
import sys
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

# 绕过代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests as _requests
_original_session_init = _requests.Session.__init__
def _patched_session_init(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    self.trust_env = False
    self.proxies = {"http": None, "https": None}
_requests.Session.__init__ = _patched_session_init

import requests

DATA_FILE = Path(__file__).parent / "data.json"
NOW = datetime.now()
HHMM = NOW.strftime("%H%M")


# ============================================================
# 1. 美股指数（AKShare + 新浪备选）
# ============================================================
def fetch_us_overnight():
    """获取美股三大指数隔夜涨跌幅 + 费城半导体"""
    results = {}
    try:
        import akshare as ak
        indices = {
            "IXIC": "纳斯达克", "DJI": "道琼斯",
            "SPX": "标普500", "SOX": "费城半导体"
        }
        for code, name in indices.items():
            try:
                df = ak.index_us_stock_sina(symbol=f".{code}")
                if df is not None and len(df) >= 2:
                    last = float(df.iloc[-1]["close"])
                    prev = float(df.iloc[-2]["close"])
                    chg = round((last - prev) / prev * 100, 2)
                    results[code] = {"name": name, "chg": chg}
            except Exception:
                pass
    except ImportError:
        print("[WARN] akshare未安装，跳过美股数据")
    except Exception as e:
        print(f"[WARN] 美股指数获取失败: {e}")

    # 备选: 新浪美股指数
    if not results:
        try:
            url = "http://hq.sinajs.cn/list=int_nasdaq,int_dji,int_sp500"
            headers = {"Referer": "http://finance.sina.com.cn"}
            resp = requests.get(url, headers=headers, timeout=8)
            resp.encoding = "gbk"
            names = {"int_nasdaq": "纳斯达克", "int_dji": "道琼斯", "int_sp500": "标普500"}
            for line in resp.text.strip().split("\n"):
                if "=" not in line:
                    continue
                var_part, data_part = line.split("=", 1)
                code = var_part.split("_hq_str_")[-1]
                fields = data_part.strip('";\n').split(",")
                if len(fields) >= 2:
                    try:
                        chg = float(fields[1])
                        results[code] = {"name": names.get(code, code), "chg": chg}
                    except ValueError:
                        pass
        except Exception as e:
            print(f"[WARN] 新浪美股备选失败: {e}")

    return results


# ============================================================
# 2. 技术指标计算函数
# ============================================================
def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 3)


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def calc_macd(closes, fast=12, slow=26, signal=9):
    """返回 (DIF, DEA, MACD柱)"""
    if len(closes) < slow + signal:
        return None, None, None

    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif[slow - 1:], signal)
    if not dea:
        return None, None, None
    macd_val = round(dif[-1], 3)
    signal_val = round(dea[-1], 3)
    hist = round(2 * (macd_val - signal_val), 3)
    return macd_val, signal_val, hist


def calc_adx(highs, lows, closes, period=14):
    """
    计算ADX(平均趋向指数)
    ADX < 22: 趋势不明确（震荡市）
    ADX 22-30: 趋势形成中
    ADX > 30: 强趋势
    """
    n = len(closes)
    if n < period * 2 + 1:
        return None

    # True Range
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    # Wilder平滑
    def wilder_smooth(data, period):
        if len(data) < period:
            return []
        smoothed = [sum(data[:period])]
        for i in range(period, len(data)):
            smoothed.append(smoothed[-1] - smoothed[-1] / period + data[i])
        return smoothed

    atr_s = wilder_smooth(tr_list, period)
    plus_dm_s = wilder_smooth(plus_dm_list, period)
    minus_dm_s = wilder_smooth(minus_dm_list, period)

    if not atr_s or not plus_dm_s or not minus_dm_s:
        return None

    min_len = min(len(atr_s), len(plus_dm_s), len(minus_dm_s))
    dx_list = []
    for i in range(min_len):
        if atr_s[i] == 0:
            continue
        plus_di = 100 * plus_dm_s[i] / atr_s[i]
        minus_di = 100 * minus_dm_s[i] / atr_s[i]
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx = 100 * abs(plus_di - minus_di) / di_sum
        dx_list.append(dx)

    if len(dx_list) < period:
        return None

    # ADX = DX的period日平均
    adx = sum(dx_list[-period:]) / period
    return round(adx, 1)


def calc_atr(highs, lows, closes, period=14):
    """计算ATR(平均真实波幅) — 用于预期波动率"""
    n = len(closes)
    if n < period + 1:
        return None

    tr_list = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    # 简单平均ATR
    atr = sum(tr_list[-period:]) / period
    return round(atr, 3)


# ============================================================
# 3. A股/港股/ETF 技术指标获取
# ============================================================
def fetch_a_stock_indicators(code):
    """获取A股技术指标: MA/RSI/MACD/ADX/ATR + 基础信息"""
    result = {"code": code, "ok": False}
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(NOW - timedelta(days=180)).strftime("%Y%m%d"),
            end_date=NOW.strftime("%Y%m%d"),
            adjust="qfq"
        )
        if df is None or len(df) < 30:
            return result

        closes = df["收盘"].astype(float).tolist()
        highs = df["最高"].astype(float).tolist()
        lows = df["最低"].astype(float).tolist()
        volumes = df["成交量"].astype(float).tolist()
        amounts = df["成交额"].astype(float).tolist() if "成交额" in df.columns else []
        latest = closes[-1]

        result["price"] = latest
        result["ma5"] = calc_ma(closes, 5)
        result["ma10"] = calc_ma(closes, 10)
        result["ma20"] = calc_ma(closes, 20)
        result["ma60"] = calc_ma(closes, 60)
        result["rsi"] = calc_rsi(closes, 14)
        result["macd"], result["macd_signal"], result["macd_hist"] = calc_macd(closes)
        result["adx"] = calc_adx(highs, lows, closes, 14)
        result["atr"] = calc_atr(highs, lows, closes, 14)

        # ATR占价格百分比 → 预期日波动
        if result["atr"] and latest > 0:
            result["atr_pct"] = round(result["atr"] / latest * 100, 2)

        # 量比: 今日成交量 / 过去5日均量
        if len(volumes) >= 6:
            avg_vol = sum(volumes[-6:-1]) / 5
            if avg_vol > 0:
                result["vol_ratio"] = round(volumes[-1] / avg_vol, 2)

        # 近5日涨跌幅
        if len(closes) >= 6:
            result["chg5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)

        # 日均成交额（20日）— 用于流动性排除
        if amounts and len(amounts) >= 20:
            result["avg_amount_20d"] = round(sum(amounts[-20:]) / 20, 0)
        elif amounts and len(amounts) >= 5:
            result["avg_amount_20d"] = round(sum(amounts[-5:]) / len(amounts[-5:]), 0)

        # 上市天数（用数据条数估算）
        result["trading_days"] = len(df)

        result["ok"] = True
    except ImportError:
        print("[WARN] akshare未安装")
    except Exception as e:
        print(f"[WARN] {code} 技术指标获取失败: {e}")
    return result


def fetch_hk_stock_indicators(hk_code):
    """港股技术指标（AKShare）"""
    result = {"code": hk_code, "ok": False}
    try:
        import akshare as ak
        num = hk_code.replace("HK.", "")
        df = ak.stock_hk_hist(
            symbol=num, period="daily",
            start_date=(NOW - timedelta(days=180)).strftime("%Y%m%d"),
            end_date=NOW.strftime("%Y%m%d"),
            adjust="qfq"
        )
        if df is None or len(df) < 30:
            return result

        closes = df["收盘"].astype(float).tolist()
        highs = df["最高"].astype(float).tolist()
        lows = df["最低"].astype(float).tolist()
        latest = closes[-1]

        result["price"] = latest
        result["ma5"] = calc_ma(closes, 5)
        result["ma10"] = calc_ma(closes, 10)
        result["ma20"] = calc_ma(closes, 20)
        result["ma60"] = calc_ma(closes, 60)
        result["rsi"] = calc_rsi(closes, 14)
        result["macd"], result["macd_signal"], result["macd_hist"] = calc_macd(closes)
        result["adx"] = calc_adx(highs, lows, closes, 14)
        result["atr"] = calc_atr(highs, lows, closes, 14)

        if result["atr"] and latest > 0:
            result["atr_pct"] = round(result["atr"] / latest * 100, 2)

        if len(closes) >= 6:
            result["chg5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)

        result["ok"] = True
    except Exception as e:
        print(f"[WARN] {hk_code} 港股指标获取失败: {e}")
    return result


def fetch_etf_indicators(code):
    """ETF技术指标 — 与A股同源，但不做ST/次新排除"""
    result = {"code": code, "ok": False, "is_etf": True}
    try:
        import akshare as ak
        df = ak.fund_etf_hist_sina(symbol=f"sz{code}" if code.startswith("1") else f"sh{code}")
        if df is None or len(df) < 30:
            # 降级: 尝试A股接口
            return fetch_a_stock_indicators(code)

        closes = df["close"].astype(float).tolist()
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        latest = closes[-1]

        result["price"] = latest
        result["ma5"] = calc_ma(closes, 5)
        result["ma10"] = calc_ma(closes, 10)
        result["ma20"] = calc_ma(closes, 20)
        result["ma60"] = calc_ma(closes, 60)
        result["rsi"] = calc_rsi(closes, 14)
        result["macd"], result["macd_signal"], result["macd_hist"] = calc_macd(closes)
        result["adx"] = calc_adx(highs, lows, closes, 14)
        result["atr"] = calc_atr(highs, lows, closes, 14)

        if result["atr"] and latest > 0:
            result["atr_pct"] = round(result["atr"] / latest * 100, 2)

        if len(closes) >= 6:
            result["chg5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)

        result["ok"] = True
    except Exception:
        # ETF sina接口失败，降级到A股接口
        fallback = fetch_a_stock_indicators(code)
        fallback["is_etf"] = True
        return fallback
    return result


# ============================================================
# 4. 硬排除规则（"排除比选股更重要"）
# ============================================================
def should_exclude(ind, stock_name=""):
    """
    检查是否应该硬排除。
    返回 (should_exclude: bool, reason: str)
    """
    name = stock_name.upper()

    # ST / *ST
    if "ST" in name or "退" in stock_name:
        return True, "ST/退市股，风险极高"

    # 次新股: 上市不足60个交易日
    trading_days = ind.get("trading_days", 999)
    if trading_days < 60:
        return True, f"上市仅{trading_days}个交易日，波动不可预测"

    # 低流动性: 20日日均成交额 < 5000万
    avg_amount = ind.get("avg_amount_20d", None)
    if avg_amount is not None and avg_amount < 50_000_000:
        amt_str = f"{avg_amount / 10000:.0f}万"
        return True, f"日均成交额仅{amt_str}，流动性不足"

    return False, ""


# ============================================================
# 5. 乘法因子信号判定（v2核心）
# ============================================================
def judge_signal_v2(ind, us_data=None, is_premarket=False, enhancement=None):
    """
    乘法因子模型 — 核心思路:
    每个因子输出一个系数(0.0~2.0), 基线=1.0
      >1.0 表示看多加成
      <1.0 表示看空压制
      =0.0 表示一票否决

    最终 score = f1 * f2 * f3 * ... * fn
    score > 1.3 → buy
    score < 0.7 → risk
    else → neutral

    优势: 单个因子极端值(如RSI超买=0.3)可以直接压制整体,
    不会被其他因子的加分抵消(加法模型的缺陷)。

    enhancement: 信号增强数据包(来自data-sources.py)
      - northbound: 北向资金持仓变化
      - unlock: 解禁数据
      - on_dragon_tiger: 是否上龙虎榜

    返回 (signal, note, confidence, factors_detail)
    """
    if not ind.get("ok"):
        return "neutral", "数据不足，暂无信号", "低", {}

    price = ind.get("price", 0)
    ma5 = ind.get("ma5")
    ma10 = ind.get("ma10")
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")
    rsi = ind.get("rsi")
    macd_hist = ind.get("macd_hist")
    vol_ratio = ind.get("vol_ratio")
    chg5d = ind.get("chg5d", 0)
    adx = ind.get("adx")
    atr_pct = ind.get("atr_pct")

    factors = {}   # 因子名 → 系数
    reasons = []

    # --- F1: 均线系统 (权重最大: 0.3~1.8) ---
    if ma5 and ma10 and ma20:
        if price > ma5 > ma10 > ma20:
            factors["ma"] = 1.8
            reasons.append("多头排列")
        elif price < ma5 < ma10 < ma20:
            factors["ma"] = 0.3
            reasons.append("空头排列")
        elif price > ma20:
            factors["ma"] = 1.2
            reasons.append("站上MA20")
        elif price < ma20:
            factors["ma"] = 0.7
            reasons.append("跌破MA20")
        else:
            factors["ma"] = 1.0
    else:
        factors["ma"] = 1.0

    # MA60加成/压制
    if ma60 and price:
        if price > ma60:
            factors["ma60"] = 1.15
        else:
            factors["ma60"] = 0.85
            reasons.append("跌破MA60")
    else:
        factors["ma60"] = 1.0

    # --- F2: RSI (0.3~1.5) ---
    if rsi is not None:
        if rsi < 25:
            factors["rsi"] = 1.5
            reasons.append(f"RSI深度超卖({rsi})")
        elif rsi < 35:
            factors["rsi"] = 1.25
            reasons.append(f"RSI偏低({rsi})")
        elif rsi > 80:
            factors["rsi"] = 0.3
            reasons.append(f"RSI严重超买({rsi})")
        elif rsi > 70:
            factors["rsi"] = 0.6
            reasons.append(f"RSI超买({rsi})")
        elif rsi > 60:
            factors["rsi"] = 0.85
            reasons.append(f"RSI偏高({rsi})")
        else:
            factors["rsi"] = 1.0
    else:
        factors["rsi"] = 1.0

    # --- F3: MACD (0.7~1.3) ---
    if macd_hist is not None:
        if macd_hist > 0:
            factors["macd"] = 1.2
            reasons.append("MACD红柱")
        else:
            factors["macd"] = 0.8
            reasons.append("MACD绿柱")
    else:
        factors["macd"] = 1.0

    # --- F4: 量比 (0.7~1.3) ---
    if vol_ratio is not None:
        if vol_ratio > 2:
            if chg5d > 0:
                factors["vol"] = 1.3
                reasons.append(f"放量上攻(量比{vol_ratio})")
            else:
                factors["vol"] = 0.7
                reasons.append(f"放量下跌(量比{vol_ratio})")
        elif vol_ratio < 0.5:
            factors["vol"] = 0.9
            reasons.append("极度缩量")
        else:
            factors["vol"] = 1.0
    else:
        factors["vol"] = 1.0

    # --- F5: 近5日动量 (0.7~1.2) ---
    if chg5d > 10:
        factors["momentum"] = 0.7
        reasons.append(f"5日涨{chg5d}%，短线获利盘重")
    elif chg5d > 5:
        factors["momentum"] = 0.85
        reasons.append(f"5日涨{chg5d}%，注意回调")
    elif chg5d < -10:
        factors["momentum"] = 1.2
        reasons.append(f"5日跌{chg5d}%，关注超跌反弹")
    elif chg5d < -5:
        factors["momentum"] = 1.1
        reasons.append(f"5日跌{chg5d}%，或有修复")
    else:
        factors["momentum"] = 1.0

    # --- F6: 美股隔夜影响（盘前权重更大）---
    if us_data and is_premarket:
        sox = us_data.get("SOX", {})
        nasdaq = us_data.get("IXIC", us_data.get("int_nasdaq", {}))
        us_factor = 1.0
        if sox.get("chg", 0) < -3:
            us_factor *= 0.6
            reasons.append(f"费城半导体暴跌{sox['chg']}%")
        elif sox.get("chg", 0) < -1.5:
            us_factor *= 0.8
            reasons.append(f"费城半导体跌{sox['chg']}%")
        elif sox.get("chg", 0) > 2:
            us_factor *= 1.2
            reasons.append(f"费城半导体涨{sox['chg']}%")

        if nasdaq.get("chg", 0) < -2:
            us_factor *= 0.8
            reasons.append(f"纳指跌{nasdaq['chg']}%")
        elif nasdaq.get("chg", 0) > 2:
            us_factor *= 1.15
            reasons.append(f"纳指涨{nasdaq['chg']}%")

        factors["us_overnight"] = round(us_factor, 2)
    else:
        factors["us_overnight"] = 1.0

    # --- F7: 北向资金 (0.7~1.25) ---
    if enhancement and enhancement.get("northbound", {}).get("ok"):
        nb = enhancement["northbound"]
        if nb.get("trend") == "增持":
            factors["northbound"] = 1.2
            reasons.append(f"北向增持(+{nb.get('hold_chg',0)}%)")
        elif nb.get("trend") == "减持":
            hold_chg = abs(nb.get("hold_chg", 0))
            if hold_chg > 0.5:
                factors["northbound"] = 0.7
                reasons.append(f"北向大幅减持(-{hold_chg}%)")
            else:
                factors["northbound"] = 0.85
                reasons.append(f"北向小幅减持(-{hold_chg}%)")
        else:
            factors["northbound"] = 1.0
    else:
        factors["northbound"] = 1.0

    # --- F8: 解禁压力 (0.5~1.0) ---
    if enhancement and enhancement.get("unlock", {}).get("has_unlock"):
        unlock = enhancement["unlock"]
        ratio = unlock.get("ratio", 0)
        risk_lvl = unlock.get("risk_level", "低")
        if risk_lvl == "高":
            factors["unlock"] = 0.5
            reasons.append(f"30天内解禁{ratio}%(高风险)")
        elif risk_lvl == "中":
            factors["unlock"] = 0.75
            reasons.append(f"30天内解禁{ratio}%")
        else:
            factors["unlock"] = 0.9
    else:
        factors["unlock"] = 1.0

    # --- F9: 龙虎榜(信息性，不直接参与评分但记录) ---
    if enhancement and enhancement.get("on_dragon_tiger"):
        detail = enhancement.get("dragon_tiger_detail", {})
        net_buy = detail.get("net_buy", 0)
        if net_buy > 0:
            factors["dragon_tiger"] = 1.1
            reasons.append("龙虎榜净买入")
        else:
            factors["dragon_tiger"] = 0.9
            reasons.append("龙虎榜净卖出")
    else:
        factors["dragon_tiger"] = 1.0

    # --- 计算总分 ---
    score = 1.0
    for f in factors.values():
        score *= f
    score = round(score, 3)

    # --- ADX趋势过滤器 ---
    adx_note = ""
    if adx is not None:
        if adx < 22:
            # 震荡市: 无论多空信号都降级为中性
            adx_note = f"ADX={adx}(震荡市)"
            if score > 1.3 or score < 0.7:
                reasons.append(f"{adx_note}，信号降级")
                score = 1.0  # 强制中性
        elif adx > 30:
            adx_note = f"ADX={adx}(强趋势)"
            # 强趋势时放大信号
            if score > 1.0:
                score *= 1.1
            elif score < 1.0:
                score *= 0.9
            score = round(score, 3)

    # --- 信号判定 ---
    if score >= 1.3:
        signal = "buy"
    elif score <= 0.7:
        signal = "risk"
    else:
        signal = "neutral"

    # --- 置信度评估 ---
    # 看因子方向一致性: 同向因子越多，置信度越高
    bullish = sum(1 for f in factors.values() if f > 1.05)
    bearish = sum(1 for f in factors.values() if f < 0.95)
    total_factors = len(factors)

    if signal == "buy":
        consistency = bullish / total_factors if total_factors > 0 else 0
    elif signal == "risk":
        consistency = bearish / total_factors if total_factors > 0 else 0
    else:
        consistency = 0

    if consistency >= 0.6:
        confidence = "高"
    elif consistency >= 0.4:
        confidence = "中"
    else:
        confidence = "低"

    # --- 构建note ---
    # 预期波动提示
    vol_hint = ""
    if atr_pct:
        if atr_pct > 4:
            vol_hint = f"预期日波动±{atr_pct}%(高)"
        elif atr_pct > 2:
            vol_hint = f"预期日波动±{atr_pct}%"
        else:
            vol_hint = f"预期日波动±{atr_pct}%(低)"

    # 取前3个关键reason + 波动提示
    key_reasons = reasons[:3]
    if adx_note and adx_note not in " ".join(key_reasons):
        key_reasons.append(adx_note)
    if vol_hint:
        key_reasons.append(vol_hint)

    note = "；".join(key_reasons) if key_reasons else "指标中性，观望为主"

    return signal, note, confidence, factors


# ============================================================
# 6. 生成层级总结
# ============================================================
def generate_layer_summary(layer_id, layer_name, stocks_signals, us_data=None):
    """根据层内所有股票信号生成该层总结"""
    if not stocks_signals:
        return ""

    buy_count = sum(1 for s in stocks_signals if s.get("signal") == "buy")
    risk_count = sum(1 for s in stocks_signals if s.get("signal") == "risk")
    total = len(stocks_signals)

    chgs = [s.get("chg5d", 0) for s in stocks_signals if s.get("chg5d") is not None]
    avg_chg = round(sum(chgs) / len(chgs), 2) if chgs else 0

    # 高置信度信号数
    high_conf = sum(1 for s in stocks_signals if s.get("confidence") == "高")

    parts = []
    parts.append(f"{layer_name}板块{total}只标的")

    if buy_count > risk_count:
        parts.append(f"整体偏多（{buy_count}只建仓信号")
        if high_conf > 0:
            parts[-1] += f"，{high_conf}只高置信"
        parts[-1] += "）"
    elif risk_count > buy_count:
        parts.append(f"整体偏弱（{risk_count}只风险信号）")
    else:
        parts.append("方向不明确，保持观望")

    if avg_chg > 3:
        parts.append(f"近5日均涨{avg_chg}%，注意追高风险")
    elif avg_chg < -3:
        parts.append(f"近5日均跌{avg_chg}%，关注超跌反弹")

    if us_data:
        sox = us_data.get("SOX", {})
        if sox.get("chg", 0) < -2 and layer_id in ("L2", "L3"):
            parts.append("费城半导体走弱，算力链承压")
        elif sox.get("chg", 0) > 2 and layer_id in ("L2", "L3"):
            parts.append("费城半导体走强，提振算力链")

    return "，".join(parts) + "。"


# ============================================================
# 7. 主流程
# ============================================================
def is_etf_code(code):
    """判断是否为ETF代码"""
    # A股ETF: 15xxxx(深) / 51xxxx(沪) / 56xxxx(沪)
    if len(code) == 6 and code[:2] in ("15", "51", "56", "58", "16"):
        return True
    return False


def main():
    if not DATA_FILE.exists():
        print("[ERROR] data.json不存在")
        sys.exit(1)

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    time_tag = NOW.strftime("%H:%M")
    is_premarket = HHMM < "0925"

    print(f"[信号v2] 开始生成 — {NOW.strftime('%Y-%m-%d %H:%M')} {'(盘前)' if is_premarket else '(盘中)'}")
    print(f"[信号v2] 乘法因子模型 + ADX过滤 + 硬排除 + 北向/解禁/龙虎榜")

    # 导入信号增强模块
    try:
        from importlib.util import spec_from_file_location, module_from_spec
        ds_path = Path(__file__).parent / "data-sources.py"
        spec = spec_from_file_location("data_sources", ds_path)
        ds = module_from_spec(spec)
        spec.loader.exec_module(ds)
        has_ds = True
        print("[数据源] 六层数据源模块已加载")
    except Exception as e:
        has_ds = False
        print(f"[数据源] 增强模块不可用: {e}")

    # 预加载市场级增强数据(只加载一次，所有股票共用)
    northbound_flow = {}
    dragon_tiger_codes = set()
    unlock_map = {}
    if has_ds:
        try:
            northbound_flow = ds.fetch_northbound_flow()
            if northbound_flow.get("ok"):
                print(f"[北向] {northbound_flow.get('trend','')} 净流入{northbound_flow.get('net_flow',0)}亿 连续{northbound_flow.get('consecutive',0)}天")
        except Exception as e:
            print(f"[北向] 获取失败: {e}")

        try:
            lhb = ds.fetch_dragon_tiger()
            dragon_tiger_codes = {item["code"] for item in lhb}
            if lhb:
                print(f"[龙虎榜] 今日{len(lhb)}只上榜")
        except Exception as e:
            print(f"[龙虎榜] 获取失败: {e}")

        try:
            unlocks = ds.fetch_unlock_schedule(30)
            unlock_map = {u["code"]: u for u in unlocks}
            if unlocks:
                print(f"[解禁] 未来30天{len(unlocks)}只有解禁")
        except Exception as e:
            print(f"[解禁] 获取失败: {e}")

    # Step 1: 美股隔夜数据
    us_data = {}
    if is_premarket or HHMM <= "1000":
        us_data = fetch_us_overnight()
        if us_data:
            names = [f"{v['name']}{'+' if v['chg']>0 else ''}{v['chg']}%" for v in us_data.values()]
            print(f"[美股] {', '.join(names)}")
        else:
            print("[美股] 无数据")

    def apply_signal(stock, stocks_signals=None):
        """为单个公开标的补技术信号；失败时诚实降级，不阻断整页刷新。"""
        code = stock["code"]
        name = stock.get("name", code)

        # --- 获取技术指标 ---
        if code.startswith("HK."):
            ind = fetch_hk_stock_indicators(code)
        elif is_etf_code(code):
            ind = fetch_etf_indicators(code)
        else:
            ind = fetch_a_stock_indicators(code)

        if not ind.get("ok") and (code.startswith("HK.") or is_etf_code(code)):
            cv = float(stock.get("cv") or 0)
            if cv <= -3:
                signal, confidence = "risk", "低"
                note = f"历史指标未取到；实时跌幅{cv:g}%，先按防守处理"
            elif cv >= 5:
                signal, confidence = "neutral", "低"
                note = f"历史指标未取到；实时涨幅+{cv:g}%，不追高，等回撤"
            else:
                signal, confidence = "neutral", "低"
                note = f"历史指标未取到；实时涨跌{cv:g}%，维持观察"
            stock["signal"] = signal
            stock["signalNote"] = note
            stock["signalTime"] = time_tag
            stock["confidence"] = confidence
            if stocks_signals is not None:
                stocks_signals.append({"signal": signal, "chg5d": 0, "confidence": confidence})
            label = {"buy": "🟢建仓", "risk": "🔴风险", "neutral": "⚪中性"}[signal]
            print(f"  [{code}] {name}: {label}({confidence}·) quote-fallback — {note}")
            return False

        # --- 硬排除检查（港股和ETF跳过）---
        if not code.startswith("HK.") and not is_etf_code(code):
            excluded, excl_reason = should_exclude(ind, name)
            if excluded:
                stock["signal"] = "risk"
                stock["signalNote"] = f"[排除] {excl_reason}"
                stock["signalTime"] = time_tag
                stock["confidence"] = "高"
                if stocks_signals is not None:
                    stocks_signals.append({"signal": "risk", "chg5d": 0, "confidence": "高"})
                print(f"  [{code}] {name}: 🚫排除 — {excl_reason}")
                return True

        # --- 构建信号增强包 ---
        enhancement = None
        if has_ds and not code.startswith("HK.") and not is_etf_code(code):
            enhancement = {"northbound": {"ok": False}, "unlock": {"has_unlock": False}, "on_dragon_tiger": False}
            # 北向个股持仓(A股主板才查)
            try:
                nb_stock = ds.fetch_northbound_stock_holding(code)
                if nb_stock.get("ok"):
                    enhancement["northbound"] = nb_stock
            except:
                pass
            # 解禁(从预加载map查)
            if code in unlock_map:
                u = unlock_map[code]
                ratio = u.get("unlock_ratio", 0)
                enhancement["unlock"] = {
                    "has_unlock": True,
                    "date": u.get("date", ""),
                    "ratio": ratio,
                    "risk_level": "高" if ratio > 5 else ("中" if ratio > 1 else "低"),
                }
            # 龙虎榜(从预加载set查)
            if code in dragon_tiger_codes:
                enhancement["on_dragon_tiger"] = True

        # --- 乘法因子信号判定 ---
        signal, note, confidence, factors = judge_signal_v2(ind, us_data, is_premarket, enhancement)
        stock["signal"] = signal
        stock["signalNote"] = note
        stock["signalTime"] = time_tag
        stock["confidence"] = confidence

        if stocks_signals is not None:
            stocks_signals.append({
                "signal": signal,
                "chg5d": ind.get("chg5d", 0),
                "confidence": confidence
            })

        label = {"buy": "🟢建仓", "risk": "🔴风险", "neutral": "⚪中性"}[signal]
        conf_icon = {"高": "★", "中": "☆", "低": "·"}[confidence]
        # 打印因子详情
        f_str = " × ".join(f"{k}={v}" for k, v in factors.items() if v != 1.0)
        score = 1.0
        for v in factors.values():
            score *= v
        print(f"  [{code}] {name}: {label}({confidence}{conf_icon}) score={score:.2f} [{f_str}] — {note}")
        return False

    # Step 2: 遍历每层
    total_excluded = 0
    for layer in data.get("layers", []):
        layer_id = layer["id"]
        layer_name = layer.get("name", layer_id)
        stocks_signals = []

        for stock in layer.get("stocks", []):
            if apply_signal(stock, stocks_signals):
                total_excluded += 1

        # 生成层级总结
        summary = generate_layer_summary(layer_id, layer_name, stocks_signals, us_data)
        if summary:
            layer["summary"] = summary
            layer["summaryTime"] = time_tag
            print(f"  [{layer_id}] 总结: {summary}")

    # Step 2.5: 补齐首页顶部 ETF 与港股列表。它们不参与层级 summary, 但前端会展示信号徽标。
    if data.get("etfs"):
        print("[信号v2] 补齐首页 ETF 信号...")
        for item in data.get("etfs", []):
            apply_signal(item)

    if data.get("hk"):
        print("[信号v2] 补齐港股列表信号...")
        for item in data.get("hk", []):
            apply_signal(item)

    # Step 3: 写回 data.json
    data["signalUpdateTime"] = NOW.strftime("%Y-%m-%d %H:%M:%S")
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n[信号v2] 完成 — 排除{total_excluded}只 — 已写入 data.json")


if __name__ == "__main__":
    main()
