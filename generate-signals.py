#!/usr/bin/env python3
"""
OpenClaw 信号生成器
每日调度:
  06:00 — 盘前信号（美股隔夜 + 公告/新闻评估）
  09:25 — 集合竞价信号（开盘价 vs 均线，量比）
  10:00/10:30/11:00/11:30 — 盘中更新（技术指标实时）
  13:00/13:30/14:00/14:30/15:00 — 午后更新
  15:15 — A股收盘最终信号
  16:15 — 港股收盘最终信号

数据来源:
  - AKShare: 历史K线(MA/RSI/MACD)、美股指数
  - 新浪HTTP: 实时价格/量比

信号等级:
  buy(建仓) — 技术面+基本面共振向好
  neutral(中性) — 无明确方向
  risk(风险) — 技术面走弱或外部利空

输出: 写入 data.json 的 signal/signalNote/signalTime + layer.summary/summaryTime
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
# 1. 美股指数（AKShare）— 盘前6:00使用
# ============================================================
def fetch_us_overnight():
    """获取美股三大指数隔夜涨跌幅 + 费城半导体"""
    results = {}
    try:
        import akshare as ak
        # 纳指、标普、道琼斯、费城半导体
        indices = {
            "IXIC": "纳斯达克",
            "DJI": "道琼斯",
            "SPX": "标普500",
            "SOX": "费城半导体"
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
# 2. A股技术指标（AKShare历史K线 → 计算MA/RSI/MACD）
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
    """返回 (MACD, Signal, Histogram)"""
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


def fetch_a_stock_indicators(code):
    """获取A股技术指标: MA5/MA10/MA20/MA60, RSI14, MACD, 最新价/量比"""
    result = {"code": code, "ok": False}
    try:
        import akshare as ak
        # 日K线最近120天
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(NOW - timedelta(days=180)).strftime("%Y%m%d"),
            end_date=NOW.strftime("%Y%m%d"),
            adjust="qfq"
        )
        if df is None or len(df) < 30:
            return result

        closes = df["收盘"].astype(float).tolist()
        volumes = df["成交量"].astype(float).tolist()
        latest = closes[-1]

        result["price"] = latest
        result["ma5"] = calc_ma(closes, 5)
        result["ma10"] = calc_ma(closes, 10)
        result["ma20"] = calc_ma(closes, 20)
        result["ma60"] = calc_ma(closes, 60)
        result["rsi"] = calc_rsi(closes, 14)
        result["macd"], result["macd_signal"], result["macd_hist"] = calc_macd(closes)

        # 量比: 今日成交量 / 过去5日均量
        if len(volumes) >= 6:
            avg_vol = sum(volumes[-6:-1]) / 5
            if avg_vol > 0:
                result["vol_ratio"] = round(volumes[-1] / avg_vol, 2)

        # 近5日涨跌幅
        if len(closes) >= 6:
            result["chg5d"] = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2)

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
        latest = closes[-1]

        result["price"] = latest
        result["ma5"] = calc_ma(closes, 5)
        result["ma10"] = calc_ma(closes, 10)
        result["ma20"] = calc_ma(closes, 20)
        result["rsi"] = calc_rsi(closes, 14)
        result["macd"], result["macd_signal"], result["macd_hist"] = calc_macd(closes)
        result["ok"] = True
    except Exception as e:
        print(f"[WARN] {hk_code} 港股指标获取失败: {e}")
    return result


# ============================================================
# 3. 信号判定规则
# ============================================================
def judge_signal(ind, us_data=None, is_premarket=False):
    """
    根据技术指标+美股隔夜，判定信号
    返回 (signal, note)
    signal: "buy" / "neutral" / "risk"
    """
    if not ind.get("ok"):
        return "neutral", "数据不足，暂无信号"

    price = ind.get("price", 0)
    ma5 = ind.get("ma5")
    ma10 = ind.get("ma10")
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")
    rsi = ind.get("rsi")
    macd_hist = ind.get("macd_hist")
    vol_ratio = ind.get("vol_ratio")
    chg5d = ind.get("chg5d", 0)

    score = 0  # 正=看多, 负=看空
    reasons = []

    # --- 均线系统 ---
    if ma5 and ma10 and ma20:
        if price > ma5 > ma10 > ma20:
            score += 3
            reasons.append("多头排列")
        elif price < ma5 < ma10 < ma20:
            score -= 3
            reasons.append("空头排列")
        elif price > ma20:
            score += 1
            reasons.append("站上MA20")
        elif price < ma20:
            score -= 1
            reasons.append("跌破MA20")

    if ma60 and price:
        if price > ma60:
            score += 1
        else:
            score -= 1
            reasons.append("跌破MA60")

    # --- RSI ---
    if rsi is not None:
        if rsi < 30:
            score += 2
            reasons.append(f"RSI超卖({rsi})")
        elif rsi < 40:
            score += 1
            reasons.append(f"RSI偏低({rsi})")
        elif rsi > 75:
            score -= 2
            reasons.append(f"RSI超买({rsi})")
        elif rsi > 65:
            score -= 1
            reasons.append(f"RSI偏高({rsi})")

    # --- MACD ---
    if macd_hist is not None:
        if macd_hist > 0:
            score += 1
            reasons.append("MACD红柱")
        else:
            score -= 1
            reasons.append("MACD绿柱")

    # --- 量比 ---
    if vol_ratio is not None:
        if vol_ratio > 2:
            reasons.append(f"放量(量比{vol_ratio})")
            # 放量方向取决于涨跌
            if chg5d > 0:
                score += 1
            else:
                score -= 1
        elif vol_ratio < 0.5:
            reasons.append("缩量")

    # --- 近5日涨跌 ---
    if chg5d > 8:
        score -= 1
        reasons.append(f"5日涨{chg5d}%，短线获利盘")
    elif chg5d < -8:
        score += 1
        reasons.append(f"5日跌{chg5d}%，或有超跌反弹")

    # --- 美股隔夜影响（盘前6:00权重更大）---
    if us_data and is_premarket:
        sox = us_data.get("SOX", {})
        nasdaq = us_data.get("IXIC", us_data.get("int_nasdaq", {}))
        if sox.get("chg", 0) < -2:
            score -= 2
            reasons.append(f"费城半导体隔夜跌{sox['chg']}%")
        elif sox.get("chg", 0) > 2:
            score += 1
            reasons.append(f"费城半导体隔夜涨{sox['chg']}%")
        if nasdaq.get("chg", 0) < -1.5:
            score -= 1
            reasons.append(f"纳指跌{nasdaq['chg']}%")
        elif nasdaq.get("chg", 0) > 1.5:
            score += 1
            reasons.append(f"纳指涨{nasdaq['chg']}%")

    # --- 信号判定 ---
    if score >= 3:
        signal = "buy"
    elif score <= -3:
        signal = "risk"
    else:
        signal = "neutral"

    note = "；".join(reasons[:3]) if reasons else "指标中性，观望为主"
    return signal, note


# ============================================================
# 4. 生成层级总结
# ============================================================
def generate_layer_summary(layer_id, layer_name, stocks_signals, us_data=None):
    """根据层内所有股票信号生成该层总结"""
    if not stocks_signals:
        return ""

    buy_count = sum(1 for s in stocks_signals if s.get("signal") == "buy")
    risk_count = sum(1 for s in stocks_signals if s.get("signal") == "risk")
    neutral_count = sum(1 for s in stocks_signals if s.get("signal") == "neutral")
    total = len(stocks_signals)

    # 整体涨跌
    chgs = [s.get("chg5d", 0) for s in stocks_signals if s.get("chg5d") is not None]
    avg_chg = round(sum(chgs) / len(chgs), 2) if chgs else 0

    parts = []
    parts.append(f"{layer_name}板块{total}只标的")

    if buy_count > risk_count:
        parts.append(f"整体偏多（{buy_count}只建仓信号）")
    elif risk_count > buy_count:
        parts.append(f"整体偏弱（{risk_count}只风险信号）")
    else:
        parts.append("方向不明确，保持观望")

    if avg_chg > 3:
        parts.append(f"近5日均涨{avg_chg}%，注意追高风险")
    elif avg_chg < -3:
        parts.append(f"近5日均跌{avg_chg}%，关注超跌反弹机会")

    # 美股影响
    if us_data:
        sox = us_data.get("SOX", {})
        if sox.get("chg", 0) < -2 and layer_id in ("L2", "L3"):
            parts.append(f"费城半导体隔夜走弱，半导体/通信链承压")
        elif sox.get("chg", 0) > 2 and layer_id in ("L2", "L3"):
            parts.append(f"费城半导体隔夜走强，提振算力链情绪")

    return "，".join(parts) + "。"


# ============================================================
# 5. 主流程
# ============================================================
def main():
    if not DATA_FILE.exists():
        print("[ERROR] data.json不存在")
        sys.exit(1)

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    time_tag = NOW.strftime("%H:%M")
    is_premarket = HHMM < "0925"

    print(f"[信号] 开始生成 — {NOW.strftime('%Y-%m-%d %H:%M')} {'(盘前)' if is_premarket else '(盘中)'}")

    # Step 1: 美股隔夜数据
    us_data = {}
    if is_premarket or HHMM <= "1000":
        us_data = fetch_us_overnight()
        if us_data:
            names = [f"{v['name']}{'+' if v['chg']>0 else ''}{v['chg']}%" for v in us_data.values()]
            print(f"[美股] {', '.join(names)}")
        else:
            print("[美股] 无数据")

    # Step 2: 遍历每层，获取技术指标 + 生成信号
    for layer in data.get("layers", []):
        layer_id = layer["id"]
        layer_name = layer.get("name", layer_id)
        stocks_signals = []

        for stock in layer.get("stocks", []):
            code = stock["code"]
            name = stock.get("name", code)

            # 获取技术指标
            if code.startswith("HK."):
                ind = fetch_hk_stock_indicators(code)
            elif code.startswith("1") and len(code) == 6:
                # ETF — 简化处理，给中性
                stock["signal"] = "neutral"
                stock["signalNote"] = "ETF跟踪指数，参考板块整体方向"
                stock["signalTime"] = time_tag
                stocks_signals.append({"signal": "neutral", "chg5d": 0})
                print(f"  [{code}] {name}: 中性(ETF)")
                continue
            else:
                ind = fetch_a_stock_indicators(code)

            # 判定信号
            signal, note = judge_signal(ind, us_data, is_premarket)
            stock["signal"] = signal
            stock["signalNote"] = note
            stock["signalTime"] = time_tag

            stocks_signals.append({
                "signal": signal,
                "chg5d": ind.get("chg5d", 0)
            })

            label = {"buy": "🟢建仓", "risk": "🔴风险", "neutral": "⚪中性"}[signal]
            print(f"  [{code}] {name}: {label} — {note}")

        # 生成层级总结
        summary = generate_layer_summary(layer_id, layer_name, stocks_signals, us_data)
        if summary:
            layer["summary"] = summary
            layer["summaryTime"] = time_tag
            print(f"  [{layer_id}] 总结: {summary}")

    # Step 3: 写回 data.json
    data["signalUpdateTime"] = NOW.strftime("%Y-%m-%d %H:%M:%S")
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[信号] 完成，已写入 data.json (信号更新时间: {time_tag})")


if __name__ == "__main__":
    main()
