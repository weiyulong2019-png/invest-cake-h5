#!/usr/bin/env python3
"""
投资工作室H5 — 数据刷新脚本
调用妙想API获取A股行情 + 富途OpenD获取港股行情，输出data.json供H5消费。

使用方式:
  export MX_APIKEY=your_key_here
  python3 refresh-data.py          # A股+港股
  python3 refresh-data.py --hk     # 仅港股
  python3 refresh-data.py --a      # 仅A股

输出: 同目录下 data.json
"""

import os
import sys
import json
import time
import argparse
import requests
import subprocess
from datetime import datetime
from pathlib import Path

# ========== 配置 ==========
MX_APIKEY = os.environ.get("MX_APIKEY", "")
MX_BASE = "https://mkapi2.dfcfs.com/finskillshub"
FUTU_OPEND_HOST = os.environ.get("FUTU_HOST", "127.0.0.1")
FUTU_OPEND_PORT = int(os.environ.get("FUTU_PORT", "11111"))
FUTU_SNAPSHOT_SCRIPT = os.path.expanduser("~/.openclaw/skills/futuapi/scripts/quote/get_snapshot.py")
OUTPUT_DIR = Path(__file__).parent
OUTPUT_FILE = OUTPUT_DIR / "data.json"

# 持仓标的 — 与H5五层蛋糕对应
HOLDINGS = {
    "L1": {
        "name": "能源基建", "sub": "电网 / 特高压 / 电力设备",
        "color": "#E8740A",
        "stocks": [
            {"code": "159326", "name": "电网设备ETF", "sector": "电网", "held": True},
            {"code": "300390", "name": "天华新能", "sector": "锂电上游", "held": True},
        ]
    },
    "L2": {
        "name": "算力芯片", "sub": "GPU / HBM / 半导体材料 / 液冷 / 服务器",
        "color": "#E03B3B",
        "stocks": [
            {"code": "002409", "name": "雅克科技", "sector": "半导体材料", "held": True, "pick": True},
            {"code": "002837", "name": "英维克", "sector": "AI液冷", "held": True, "pick": True},
            {"code": "601138", "name": "工业富联", "sector": "AI服务器", "held": True, "pick": True},
            {"code": "301217", "name": "铜冠铜箔", "sector": "电子铜箔", "held": True},
            {"code": "688008", "name": "澜起科技", "sector": "DDR5+CXL", "held": False},
        ]
    },
    "L3": {
        "name": "通信基建", "sub": "光模块 / CPO / 高速PCB / 海缆",
        "color": "#2B7FD4",
        "stocks": [
            {"code": "001389", "name": "广合科技", "sector": "高速PCB", "held": True},
            {"code": "300476", "name": "胜宏科技", "sector": "高多层PCB", "held": True},
            {"code": "600487", "name": "亨通光电", "sector": "光通信+海缆", "held": False, "pick": True},
        ]
    },
    "L4": {
        "name": "模型层", "sub": "大模型 / 训练框架 / AI原生应用",
        "color": "#8E44AD",
        "stocks": [
            {"code": "300418", "name": "昆仑万维", "sector": "AI应用/大模型", "held": True},
        ]
    },
    "L5": {
        "name": "应用层", "sub": "SaaS+AI / 互联网平台 / 消费科技",
        "color": "#0EA352",
        "stocks": []  # 港股通过富途获取，此处留空
    }
}

# ETF列表
ETFS = [
    {"code": "512890", "name": "红利低波ETF", "sector": "防御"},
    {"code": "159363", "name": "创业板AI ETF", "sector": "AI赛道"},
    {"code": "516510", "name": "云计算ETF", "sector": "AI云"},
    {"code": "159326", "name": "电网设备ETF", "sector": "电网"},
    {"code": "159338", "name": "中证A500ETF", "sector": "宽基"},
]

# 港股（富途OpenD获取实时数据）
HK_STOCKS = [
    {"code": "HK.01810", "futu": "HK.01810", "name": "小米集团-W", "sector": "港股互联网", "held": True},
    {"code": "HK.03690", "futu": "HK.03690", "name": "美团-W", "sector": "港股互联网", "held": True},
    {"code": "HK.09988", "futu": "HK.09988", "name": "阿里巴巴-W", "sector": "港股互联网", "held": True},
    {"code": "HK.01024", "futu": "HK.01024", "name": "快手-W", "sector": "港股互联网", "held": True},
]


def fetch_hk_quotes():
    """通过富途OpenD get_snapshot.py获取港股实时行情"""
    futu_codes = [s["futu"] for s in HK_STOCKS]
    print(f"  富途查询: {' '.join(futu_codes)}")

    # 方式1: 调用现有脚本
    if os.path.exists(FUTU_SNAPSHOT_SCRIPT):
        try:
            cmd = ["python3", FUTU_SNAPSHOT_SCRIPT] + futu_codes + ["--json"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return data.get("data", [])
            else:
                print(f"  [WARN] 脚本返回错误: {result.stderr[:200]}")
        except Exception as e:
            print(f"  [WARN] 脚本调用失败: {e}")

    # 方式2: 直接用futu-api SDK
    try:
        from futu import OpenQuoteContext, RET_OK
        ctx = OpenQuoteContext(host=FUTU_OPEND_HOST, port=FUTU_OPEND_PORT)
        ret, data = ctx.get_market_snapshot(futu_codes)
        ctx.close()
        if ret == RET_OK and data is not None:
            records = []
            for i in range(len(data)):
                row = data.iloc[i]
                records.append({
                    "code": str(row.get("code", "")),
                    "name": str(row.get("name", "")),
                    "last_price": float(row.get("last_price", 0)),
                    "prev_close": float(row.get("prev_close_price", 0)),
                    "pe_ttm": float(row.get("pe_ttm_ratio", 0)) if row.get("pe_ttm_ratio") else 0,
                    "market_val": float(row.get("total_market_val", 0)) if row.get("total_market_val") else 0,
                })
            return records
        else:
            print(f"  [WARN] 富途API返回错误: {data}")
    except ImportError:
        print("  [WARN] futu-api未安装，跳过港股")
    except Exception as e:
        print(f"  [WARN] 富途连接失败: {e}")

    return []


def mx_query(query: str) -> dict:
    """调用妙想选股API"""
    headers = {"Content-Type": "application/json", "apikey": MX_APIKEY}
    try:
        resp = requests.post(
            f"{MX_BASE}/api/claw/stock-screen",
            headers=headers, json={"keyword": query}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[WARN] mx_query failed: {e}")
        return {}


def parse_mx_response(result: dict) -> list:
    """解析妙想返回的选股数据为行列表"""
    try:
        data = result.get("data", {}).get("data", {})
        # 优先全量 dataList
        all_results = data.get("allResults", {}).get("result", {})
        data_list = all_results.get("dataList", [])
        columns = all_results.get("columns", [])

        if data_list and columns:
            col_map = {}
            for col in columns:
                field = col.get("field") or col.get("name") or col.get("key")
                display = col.get("displayName") or col.get("title") or col.get("label", "")
                if field:
                    col_map[str(field)] = display
            rows = []
            for item in data_list:
                row = {}
                for k, v in item.items():
                    label = col_map.get(str(k), str(k))
                    row[label] = v
                rows.append(row)
            return rows

        # 回退: 解析 partialResults markdown
        partial = data.get("partialResults", "")
        if partial:
            return parse_markdown_table(partial)
    except Exception as e:
        print(f"[WARN] parse error: {e}")
    return []


def parse_markdown_table(md: str) -> list:
    """解析markdown表格"""
    lines = [l.strip() for l in md.strip().splitlines() if l.strip()]
    if len(lines) < 3:
        return []
    headers = [c.strip() for c in lines[0].split("|") if c.strip()]
    rows = []
    for line in lines[2:]:  # skip separator
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def fetch_a_stock_quotes(codes: list, names: list) -> dict:
    """批量获取A股行情"""
    # 构造查询: "雅克科技 英维克 工业富联 最新价 涨跌幅 市盈率"
    name_str = " ".join(names[:8])  # 每批最多8个
    query = f"{name_str} 最新价 涨跌幅 动态市盈率 总市值"
    print(f"  查询: {query}")

    result = mx_query(query)
    rows = parse_mx_response(result)

    quotes = {}
    for row in rows:
        # 尝试匹配股票名称或代码
        stock_name = ""
        for key in ("股票简称", "名称", "股票名称", "简称"):
            if key in row:
                stock_name = str(row[key]).strip()
                break
        if not stock_name:
            # 取第一个非数字值作为名称
            for v in row.values():
                if isinstance(v, str) and not v.replace(".", "").replace("-", "").replace("%", "").isdigit():
                    stock_name = v
                    break

        price = ""
        chg = ""
        pe = ""
        cap = ""
        for key, val in row.items():
            kl = key.lower() if key else ""
            if "最新" in kl or "收盘" in kl or "现价" in kl:
                price = str(val)
            elif "涨跌幅" in kl:
                chg = str(val)
            elif "市盈" in kl or "pe" in kl:
                pe = str(val)
            elif "总市值" in kl or "市值" in kl:
                cap = str(val)

        if stock_name:
            quotes[stock_name] = {"price": price, "chg": chg, "pe": pe, "cap": cap}

    return quotes


def build_output(skip_a=False, skip_hk=False):
    """构建完整JSON输出
    skip_a: 跳过A股数据(--hk模式)
    skip_hk: 跳过港股数据(--a模式)
    """
    now = datetime.now()
    output = {
        "updateTime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(now.timestamp()),
        "market": {"index": "上证综指", "indexCode": "000001.SH"},
        "layers": [],
        "etfs": [],
        "hk": [],
    }

    # 获取大盘指数（A股模式才需要）
    if not skip_a and MX_APIKEY:
        print("[1/4] 获取大盘指数...")
        idx_result = mx_query("上证指数 最新价 涨跌幅")
        idx_rows = parse_mx_response(idx_result)
        if idx_rows:
            for row in idx_rows:
                for k, v in row.items():
                    if "最新" in k or "收盘" in k:
                        output["market"]["price"] = str(v)
                    elif "涨跌幅" in k:
                        output["market"]["chg"] = str(v)
    else:
        print("[1/4] 跳过大盘指数")

    # 逐层获取A股行情
    if not skip_a and MX_APIKEY:
        print("[2/4] 获取A股持仓行情...")
        for layer_id, layer_info in HOLDINGS.items():
            stocks = layer_info["stocks"]
            if not stocks:
                output["layers"].append({
                    "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                    "color": layer_info["color"], "stocks": [], "avg": 0
                })
                continue

            names = [s["name"] for s in stocks]
            quotes = fetch_a_stock_quotes([s["code"] for s in stocks], names)

            layer_stocks = []
            total_chg = 0
            count = 0
            for s in stocks:
                q = quotes.get(s["name"], {})
                price = q.get("price", "-")
                chg_str = q.get("chg", "0%")
                pe = q.get("pe", "-")
                cap = q.get("cap", "-")

                # 解析涨跌幅数值
                try:
                    cv = float(chg_str.replace("%", "").replace("+", ""))
                except:
                    cv = 0

                chg_display = f"+{chg_str}" if cv > 0 and not chg_str.startswith("+") else chg_str
                if not chg_display.endswith("%"):
                    chg_display += "%"

                layer_stocks.append({
                    "code": s["code"], "name": s["name"], "sector": s["sector"],
                    "held": s.get("held", False), "pick": s.get("pick", False),
                    "p": price, "c": chg_display, "cv": cv,
                    "pe": pe, "cap": cap
                })
                total_chg += cv
                count += 1

            avg = round(total_chg / count, 2) if count else 0
            output["layers"].append({
                "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                "color": layer_info["color"], "stocks": layer_stocks, "avg": avg
            })
            time.sleep(0.5)  # 限流
    else:
        print("[2/4] 跳过A股行情")
        # 填充空layer结构（保持H5兼容）
        for layer_id, layer_info in HOLDINGS.items():
            output["layers"].append({
                "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                "color": layer_info["color"], "stocks": [], "avg": 0
            })

    # ETF行情
    if not skip_a and MX_APIKEY:
        print("[3/4] 获取ETF行情...")
        etf_names = [e["name"] for e in ETFS]
        etf_quotes = fetch_a_stock_quotes([e["code"] for e in ETFS], etf_names)
        for e in ETFS:
            q = etf_quotes.get(e["name"], {})
            price = q.get("price", "-")
            chg_str = q.get("chg", "0%")
            try:
                cv = float(chg_str.replace("%", "").replace("+", ""))
            except:
                cv = 0
            chg_display = f"+{chg_str}" if cv > 0 and not chg_str.startswith("+") else chg_str
            if not chg_display.endswith("%"):
                chg_display += "%"
            output["etfs"].append({
                "code": e["code"], "name": e["name"], "sector": e["sector"],
                "p": price, "c": chg_display, "cv": cv
            })
    else:
        print("[3/4] 跳过ETF行情")

    # 港股（富途OpenD实时数据）
    hk_result = []
    if not skip_hk:
        print("[4/4] 获取港股行情（富途OpenD）...")
        hk_quotes = fetch_hk_quotes()
        for s in HK_STOCKS:
            # 匹配富途返回数据
            q = next((r for r in hk_quotes if r.get("code") == s["futu"]), None)
            if q:
                price = q["last_price"]
                prev = q["prev_close"]
                cv = round((price - prev) / prev * 100, 2) if prev else 0
                pe = q.get("pe_ttm", 0)
                mval = q.get("market_val", 0)
                # 市值转亿（港元）
                cap = str(round(mval / 1e8)) if mval > 0 else "-"
                chg = f"+{cv}%" if cv > 0 else f"{cv}%"
                hk_result.append({
                    "code": s["code"], "name": s["name"], "sector": s["sector"],
                    "held": s.get("held", False),
                    "p": str(round(price, 2)), "c": chg, "cv": cv,
                    "pe": str(round(pe, 1)) if pe else "-", "cap": cap
                })
            else:
                # 无数据时保留占位
                hk_result.append({
                    "code": s["code"], "name": s["name"], "sector": s["sector"],
                    "held": s.get("held", False),
                    "p": "-", "c": "0%", "cv": 0, "pe": "-", "cap": "-"
                })
    else:
        print("[4/4] 跳过港股行情")

    output["hk"] = hk_result

    # 同步港股到L5应用层
    l5 = next((l for l in output["layers"] if l["id"] == "L5"), None)
    if l5 and hk_result:
        l5["stocks"] = hk_result
        vals = [s["cv"] for s in hk_result if s["cv"] != 0]
        l5["avg"] = round(sum(vals) / len(vals), 2) if vals else 0

    return output


def main():
    parser = argparse.ArgumentParser(description="投资工作室H5数据刷新")
    parser.add_argument("--hk", action="store_true", help="仅刷新港股")
    parser.add_argument("--a", action="store_true", dest="a_only", help="仅刷新A股")
    args = parser.parse_args()

    print(f"=== 投资工作室H5 数据刷新 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    sources = []
    if not args.hk:
        if MX_APIKEY:
            sources.append("妙想(A股)")
        else:
            print("⚠️  MX_APIKEY未设置，跳过A股数据")
    if not args.a_only:
        sources.append("富途OpenD(港股)")
    print(f"数据源: {', '.join(sources) if sources else '无'}")
    print()

    data = build_output(skip_a=args.hk, skip_hk=args.a_only)

    # 写入JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 数据已写入: {OUTPUT_FILE}")
    print(f"   更新时间: {data['updateTime']}")
    stocks_count = sum(len(l['stocks']) for l in data['layers'])
    print(f"   A股标的: {stocks_count}")
    print(f"   ETF: {len(data['etfs'])}")
    hk_live = sum(1 for s in data['hk'] if s['p'] != '-')
    print(f"   港股: {len(data['hk'])} ({hk_live}个有实时数据)")


if __name__ == "__main__":
    main()
