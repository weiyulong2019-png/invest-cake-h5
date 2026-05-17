#!/usr/bin/env python3
"""
投资工作室H5 — 数据刷新脚本（双模式架构）

模式:
  python3 refresh-data.py              # auto模式：AKShare免费源，适合分钟级定时
  python3 refresh-data.py --manual     # 手动模式：妙想(问财)+新浪+富途港股
  python3 refresh-data.py --hk         # 仅港股（手动模式）
  python3 refresh-data.py --a          # 仅A股（手动模式）

数据源优先级:
  auto模式:   AKShare（免费无限制） → 继承旧数据
  manual模式: 妙想API/问财（A股主数据，含PE/市值） → 新浪HTTP（备选） → 富途OpenD（仅港股） → 继承旧数据

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

# ========== 绕过代理（eastmoney等国内源不需要翻墙） ==========
# Shadowrocket等VPN通过Network Extension在系统层拦截，环境变量无法绕过
# 必须在requests.Session层面禁用trust_env，阻止它读取系统代理配置
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

import requests as _requests
_original_session_init = _requests.Session.__init__
def _patched_session_init(self, *args, **kwargs):
    _original_session_init(self, *args, **kwargs)
    self.trust_env = False  # 不读取系统代理
    self.proxies = {"http": None, "https": None}  # 强制直连
_requests.Session.__init__ = _patched_session_init

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


def code_to_futu(code: str) -> str:
    """将A股代码转为富途格式: 600xxx->SH.600xxx, 000/001/002/003/300/301->SZ.xxx"""
    if code.startswith(("HK.", "US.", "SH.", "SZ.", "SG.")):
        return code  # 已是富途格式
    if code.startswith(("6", "5")):
        return f"SH.{code}"
    else:
        return f"SZ.{code}"


# ========== 新浪财经直连接口（备选，绕过push2.eastmoney.com） ==========

def fetch_sina_quotes(codes: list) -> dict:
    """通过新浪财经HTTP接口获取A股/指数/ETF实时行情（不走HTTPS代理）
    新浪接口格式: http://hq.sinajs.cn/list=sh600519,sz000001
    返回: {code: {"price","chg","cv","pe","cap"}, ...}
    """
    if not codes:
        return {}

    # 构造新浪代码: 6开头/5开头->sh, 其他->sz
    sina_codes = []
    code_map = {}  # sina_code -> original_code
    for code in codes:
        if code.startswith(("6", "5")):
            sc = f"sh{code}"
        elif code.startswith("0") and len(code) == 6 and code[:3] == "000":
            # 指数代码 000001 -> s_sh000001
            sc = f"s_sh{code}"
        else:
            sc = f"sz{code}"
        sina_codes.append(sc)
        code_map[sc] = code

    results = {}
    # 分批每次30个
    for i in range(0, len(sina_codes), 30):
        batch = sina_codes[i:i+30]
        url = f"http://hq.sinajs.cn/list={','.join(batch)}"
        headers = {"Referer": "http://finance.sina.com.cn"}
        try:
            resp = _requests.get(url, headers=headers, timeout=10)
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                if "=" not in line or '=""' in line:
                    continue
                # var hq_str_sh600519="贵州茅台,1820.00,...";
                parts = line.split("=", 1)
                var_name = parts[0].replace("var hq_str_", "").strip()
                data_str = parts[1].strip().strip('"').strip(";").strip('"')
                if not data_str:
                    continue
                fields = data_str.split(",")
                orig_code = code_map.get(var_name, var_name.replace("sh", "").replace("sz", "").replace("s_", ""))

                if var_name.startswith("s_"):
                    # 指数简略格式: 名称,当前点位,涨跌点数,涨跌幅,成交量,成交额
                    if len(fields) >= 4:
                        price = str(round(float(fields[1]), 2)) if fields[1] else "-"
                        cv = round(float(fields[3]), 2) if fields[3] else 0
                        results[orig_code] = {
                            "price": price, "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                            "cv": cv, "pe": "-", "cap": "-"
                        }
                else:
                    # 完整格式: 0名称,1今开,2昨收,3当前,4最高,5最低,...
                    if len(fields) >= 32:
                        price = fields[3]
                        prev_close = float(fields[2]) if fields[2] else 0
                        cur_price = float(fields[3]) if fields[3] else 0
                        if prev_close > 0 and cur_price > 0:
                            cv = round((cur_price - prev_close) / prev_close * 100, 2)
                        else:
                            cv = 0
                        # 新浪不直接提供PE和市值，留空
                        results[orig_code] = {
                            "price": str(round(cur_price, 2)) if cur_price else "-",
                            "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                            "cv": cv, "pe": "-", "cap": "-"
                        }
            if i + 30 < len(sina_codes):
                time.sleep(0.5)
        except Exception as e:
            print(f"  [WARN] 新浪接口失败: {e}")

    return results


def fetch_sina_hk(codes: list) -> dict:
    """通过新浪获取港股行情
    格式: http://hq.sinajs.cn/list=rt_hk01810
    """
    if not codes:
        return {}
    sina_codes = []
    code_map = {}
    for code in codes:
        hk_num = code.replace("HK.", "")
        sc = f"rt_hk{hk_num}"
        sina_codes.append(sc)
        code_map[sc] = code

    results = {}
    url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
    headers = {"Referer": "http://finance.sina.com.cn"}
    try:
        resp = _requests.get(url, headers=headers, timeout=10)
        resp.encoding = "gbk"
        for line in resp.text.strip().split("\n"):
            if "=" not in line or '=""' in line:
                continue
            parts = line.split("=", 1)
            var_name = parts[0].replace("var hq_str_", "").strip()
            data_str = parts[1].strip().strip('"').strip(";").strip('"')
            if not data_str:
                continue
            fields = data_str.split(",")
            orig_code = code_map.get(var_name, "")
            # 港股格式: 0简称,1英文名,2今开,3昨收,4最高,5最低,6当前价,...
            if len(fields) >= 9 and orig_code:
                cur_price = float(fields[6]) if fields[6] else 0
                prev_close = float(fields[3]) if fields[3] else 0
                if prev_close > 0 and cur_price > 0:
                    cv = round((cur_price - prev_close) / prev_close * 100, 2)
                else:
                    cv = 0
                results[orig_code] = {
                    "price": str(round(cur_price, 2)) if cur_price else "-",
                    "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                    "cv": cv, "pe": "-", "cap": "-"
                }
    except Exception as e:
        print(f"  [WARN] 新浪港股接口失败: {e}")

    return results


# ========== 腾讯行情备用源 ==========

def fetch_tencent_quotes(codes: list) -> dict:
    """腾讯行情HTTP接口 — 新浪失败时的备用源
    格式: http://qt.gtimg.cn/q=sh600519,sz000001
    返回: {code: {"price","chg","cv","pe","cap"}, ...}
    """
    if not codes:
        return {}

    tc_codes = []
    code_map = {}
    for c in codes:
        if c.startswith(("6", "5")):
            tc = f"sh{c}"
        else:
            tc = f"sz{c}"
        tc_codes.append(tc)
        code_map[tc] = c

    results = {}
    for i in range(0, len(tc_codes), 30):
        batch = tc_codes[i:i+30]
        url = f"http://qt.gtimg.cn/q={','.join(batch)}"
        try:
            resp = _requests.get(url, timeout=8)
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                if "=" not in line or "~" not in line:
                    continue
                parts = line.split("=", 1)
                fields = parts[1].strip('";\n').split("~")
                if len(fields) < 45:
                    continue
                try:
                    code = fields[2]
                    price = float(fields[3]) if fields[3] else 0
                    chg_pct = float(fields[32]) if fields[32] else 0
                    pe = float(fields[39]) if fields[39] else 0
                    cap = float(fields[45]) if len(fields) > 45 and fields[45] else 0
                    results[code] = {
                        "price": str(round(price, 2)) if price else "-",
                        "chg": f"+{chg_pct}%" if chg_pct > 0 else f"{chg_pct}%",
                        "cv": chg_pct,
                        "pe": str(round(pe, 1)) if pe > 0 else "-",
                        "cap": f"{round(cap, 1)}亿" if cap > 0 else "-",
                    }
                except (ValueError, IndexError):
                    pass
            if i + 30 < len(tc_codes):
                time.sleep(0.3)
        except Exception as e:
            print(f"  [WARN] 腾讯行情接口失败: {e}")
    return results


def fetch_tencent_hk(codes: list) -> dict:
    """腾讯港股行情备用源
    格式: http://qt.gtimg.cn/q=r_hk01810
    """
    if not codes:
        return {}

    tc_codes = []
    code_map = {}
    for c in codes:
        num = c.replace("HK.", "")
        tc = f"r_hk{num}"
        tc_codes.append(tc)
        code_map[tc] = c

    results = {}
    url = f"http://qt.gtimg.cn/q={','.join(tc_codes)}"
    try:
        resp = _requests.get(url, timeout=8)
        resp.encoding = "gbk"
        for line in resp.text.strip().split("\n"):
            if "=" not in line or "~" not in line:
                continue
            parts = line.split("=", 1)
            var_name = parts[0].split("_str_")[-1] if "_str_" in parts[0] else ""
            fields = parts[1].strip('";\n').split("~")
            if len(fields) < 10:
                continue
            orig_code = code_map.get(var_name, "")
            if not orig_code:
                # 尝试从fields提取
                for tc, oc in code_map.items():
                    if tc.replace("r_hk", "") in str(fields[:5]):
                        orig_code = oc
                        break
            if not orig_code:
                continue
            try:
                price = float(fields[3]) if fields[3] else 0
                chg_pct = float(fields[32]) if len(fields) > 32 and fields[32] else 0
                results[orig_code] = {
                    "price": str(round(price, 3)) if price else "-",
                    "chg": f"+{chg_pct}%" if chg_pct > 0 else f"{chg_pct}%",
                    "cv": chg_pct,
                    "pe": "-", "cap": "-",
                }
            except (ValueError, IndexError):
                pass
    except Exception as e:
        print(f"  [WARN] 腾讯港股接口失败: {e}")
    return results


# ========== AKShare 免费数据源（auto模式） ==========

def fetch_akshare_quotes(codes: list, names: list) -> dict:
    """通过AKShare获取A股实时行情（免费，无调用限制）
    返回: {code: {"price", "chg", "cv", "pe", "cap"}, ...}
    """
    try:
        import akshare as ak
    except ImportError:
        print("  [WARN] akshare未安装，尝试安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "akshare", "-q",
                        "--break-system-packages"], capture_output=True)
        try:
            import akshare as ak
        except ImportError:
            print("  [ERROR] akshare安装失败")
            return {}

    results = {}
    # 获取A股实时行情（东方财富源，全量）
    try:
        print("  AKShare: 获取A股实时行情...")
        df = ak.stock_zh_a_spot_em()  # 东方财富全量A股实时
        if df is not None and not df.empty:
            for code in codes:
                # AKShare代码格式: 纯6位数字
                pure_code = code.replace("SH.", "").replace("SZ.", "")
                match = df[df["代码"] == pure_code]
                if not match.empty:
                    row = match.iloc[0]
                    price = row.get("最新价", 0)
                    chg_pct = row.get("涨跌幅", 0)
                    pe = row.get("市盈率-动态", 0)
                    cap = row.get("总市值", 0)
                    # 格式化市值
                    if cap and float(cap) > 0:
                        cap_f = float(cap)
                        if cap_f >= 1e12:
                            cap_str = f"{round(cap_f / 1e12, 2)}万亿"
                        elif cap_f >= 1e8:
                            cap_str = f"{round(cap_f / 1e8, 2)}亿"
                        else:
                            cap_str = str(round(cap_f))
                    else:
                        cap_str = "-"
                    cv = round(float(chg_pct), 2) if chg_pct else 0
                    results[pure_code] = {
                        "price": str(round(float(price), 2)) if price else "-",
                        "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                        "cv": cv,
                        "pe": str(round(float(pe), 2)) if pe else "-",
                        "cap": cap_str,
                    }
            print(f"  AKShare: 匹配到 {len(results)}/{len(codes)} 个A股标的")
    except Exception as e:
        print(f"  [WARN] AKShare A股获取失败: {e}")

    return results


def fetch_akshare_etfs(codes: list) -> dict:
    """通过AKShare获取ETF实时行情"""
    try:
        import akshare as ak
    except ImportError:
        return {}

    results = {}
    try:
        print("  AKShare: 获取ETF实时行情...")
        df = ak.fund_etf_spot_em()  # 东方财富ETF实时
        if df is not None and not df.empty:
            for code in codes:
                match = df[df["代码"] == code]
                if not match.empty:
                    row = match.iloc[0]
                    price = row.get("最新价", 0)
                    chg_pct = row.get("涨跌幅", 0)
                    cv = round(float(chg_pct), 2) if chg_pct else 0
                    results[code] = {
                        "price": str(round(float(price), 2)) if price else "-",
                        "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                        "cv": cv,
                    }
            print(f"  AKShare: 匹配到 {len(results)}/{len(codes)} 个ETF")
    except Exception as e:
        print(f"  [WARN] AKShare ETF获取失败: {e}")

    return results


def fetch_akshare_index() -> dict:
    """通过AKShare获取上证综指"""
    try:
        import akshare as ak
    except ImportError:
        return {}

    try:
        df = ak.stock_zh_index_spot_em()  # 指数实时
        if df is not None and not df.empty:
            match = df[df["代码"] == "000001"]
            if not match.empty:
                row = match.iloc[0]
                price = row.get("最新价", 0)
                chg_pct = row.get("涨跌幅", 0)
                cv = round(float(chg_pct), 2) if chg_pct else 0
                return {
                    "price": str(round(float(price), 2)),
                    "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                    "cv": cv,
                }
    except Exception as e:
        print(f"  [WARN] AKShare 指数获取失败: {e}")
    return {}


def fetch_akshare_hk(codes: list) -> dict:
    """通过AKShare获取港股实时行情"""
    try:
        import akshare as ak
    except ImportError:
        return {}

    results = {}
    try:
        print("  AKShare: 获取港股实时行情...")
        df = ak.stock_hk_spot_em()  # 港股实时
        if df is not None and not df.empty:
            for code in codes:
                # HK.01810 -> 01810
                hk_code = code.replace("HK.", "")
                match = df[df["代码"] == hk_code]
                if not match.empty:
                    row = match.iloc[0]
                    price = row.get("最新价", 0)
                    chg_pct = row.get("涨跌幅", 0)
                    pe = row.get("市盈率-动态", 0)
                    cap = row.get("总市值", 0)
                    if cap and float(cap) > 0:
                        cap_f = float(cap)
                        if cap_f >= 1e12:
                            cap_str = f"{round(cap_f / 1e12, 2)}万亿"
                        elif cap_f >= 1e8:
                            cap_str = f"{round(cap_f / 1e8, 2)}亿"
                        else:
                            cap_str = str(round(cap_f))
                    else:
                        cap_str = "-"
                    cv = round(float(chg_pct), 2) if chg_pct else 0
                    results[code] = {
                        "price": str(round(float(price), 2)) if price else "-",
                        "chg": f"+{cv}%" if cv > 0 else f"{cv}%",
                        "cv": cv,
                        "pe": str(round(float(pe), 2)) if pe else "-",
                        "cap": cap_str,
                    }
            print(f"  AKShare: 匹配到 {len(results)}/{len(codes)} 个港股")
    except Exception as e:
        print(f"  [WARN] AKShare 港股获取失败: {e}")

    return results


def fetch_futu_snapshots(futu_codes: list) -> list:
    """通过富途OpenD获取行情快照（通用函数，支持A股/港股/指数/ETF）
    返回: [{"code","name","last_price","prev_close","pe_ttm","market_val"}, ...]
    """
    if not futu_codes:
        return []
    print(f"  富途查询: {' '.join(futu_codes[:8])}{'...' if len(futu_codes) > 8 else ''}")

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
        # 富途单次最多查询400个，分批
        all_records = []
        for i in range(0, len(futu_codes), 50):
            batch = futu_codes[i:i+50]
            ret, data = ctx.get_market_snapshot(batch)
            if ret == RET_OK and data is not None:
                for j in range(len(data)):
                    row = data.iloc[j]
                    all_records.append({
                        "code": str(row.get("code", "")),
                        "name": str(row.get("name", "")),
                        "last_price": float(row.get("last_price", 0)),
                        "prev_close": float(row.get("prev_close_price", 0)),
                        "pe_ttm": float(row.get("pe_ttm_ratio", 0)) if row.get("pe_ttm_ratio") else 0,
                        "market_val": float(row.get("total_market_val", 0)) if row.get("total_market_val") else 0,
                    })
            else:
                print(f"  [WARN] 富途API返回错误: {data}")
            if i + 50 < len(futu_codes):
                time.sleep(0.3)
        ctx.close()
        return all_records
    except ImportError:
        print("  [WARN] futu-api未安装")
    except Exception as e:
        print(f"  [WARN] 富途连接失败: {e}")

    return []


def fetch_hk_quotes():
    """港股行情（兼容旧调用）"""
    futu_codes = [s["futu"] for s in HK_STOCKS]
    return fetch_futu_snapshots(futu_codes)


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


def _snap_to_display(snap: dict, code: str, name: str, sector: str, held=False, pick=False):
    """将富途snapshot数据转为H5显示格式"""
    if not snap:
        return {"code": code, "name": name, "sector": sector, "held": held, "pick": pick,
                "p": "-", "c": "0%", "cv": 0, "pe": "-", "cap": "-"}
    price = snap["last_price"]
    prev = snap["prev_close"]
    cv = round((price - prev) / prev * 100, 2) if prev else 0
    pe = snap.get("pe_ttm", 0)
    mval = snap.get("market_val", 0)
    # 市值格式化
    if mval >= 1e12:
        cap = f"{round(mval / 1e12, 2)}万亿"
    elif mval >= 1e8:
        cap = f"{round(mval / 1e8, 2)}亿"
    elif mval > 0:
        cap = str(round(mval))
    else:
        cap = "-"
    chg = f"+{cv}%" if cv > 0 else f"{cv}%"
    return {
        "code": code, "name": name, "sector": sector,
        "held": held, "pick": pick,
        "p": str(round(price, 2)), "c": chg, "cv": cv,
        "pe": str(round(pe, 1)) if pe else "-", "cap": cap
    }


def build_output_auto(skip_a=False, skip_hk=False):
    """AUTO模式: 新浪财经HTTP接口为主（绕过代理），AKShare为备选
    新浪优势：HTTP直连，不受Shadowrocket HTTPS拦截影响
    AKShare优势：数据更全（PE/市值），但push2.eastmoney.com可能被代理拦截
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

    # ===== 收集所有A股代码 =====
    all_a_codes = []
    for layer_info in HOLDINGS.values():
        for s in layer_info["stocks"]:
            all_a_codes.append(s["code"])
    etf_codes = [e["code"] for e in ETFS]
    hk_codes = [s["code"] for s in HK_STOCKS]

    # ===== 第1步: 新浪接口获取（主力源） =====
    sina_index = {}
    sina_all = {}
    sina_hk = {}
    ak_quotes = {}
    ak_etfs_data = {}

    if not skip_a:
        print("[1/3] 新浪财经: A股+指数+ETF...")
        sina_index = fetch_sina_quotes(["000001"])
        sina_all = fetch_sina_quotes(all_a_codes + etf_codes)
    else:
        print("[1/3] 跳过A股")

    if not skip_hk:
        print("[2/3] 新浪财经: 港股...")
        sina_hk = fetch_sina_hk(hk_codes)
    else:
        print("[2/3] 跳过港股")

    print(f"  新浪结果: 指数={'有' if sina_index else '跳过'}, A股+ETF={len(sina_all)}/{len(all_a_codes)+len(etf_codes)}, 港股={len(sina_hk)}/{len(hk_codes)}")

    # ===== 第1.5步: 腾讯行情补充新浪缺失 =====
    if not skip_a:
        missing_a = [c for c in (all_a_codes + etf_codes) if c not in sina_all]
        if missing_a:
            print(f"[1.5/3] 腾讯行情补充{len(missing_a)}个新浪缺失...")
            tc_quotes = fetch_tencent_quotes(missing_a)
            sina_all.update(tc_quotes)
            print(f"  腾讯补充: {len(tc_quotes)}/{len(missing_a)}")

    if not skip_hk:
        missing_hk = [c for c in hk_codes if c not in sina_hk]
        if missing_hk:
            print(f"[1.5/3] 腾讯港股补充{len(missing_hk)}个缺失...")
            tc_hk = fetch_tencent_hk(missing_hk)
            sina_hk.update(tc_hk)
            print(f"  腾讯港股补充: {len(tc_hk)}/{len(missing_hk)}")

    # ===== 第2步: AKShare补充PE/市值（如果可用） =====
    if not skip_a:
        print("[3/3] AKShare补充PE/市值...")
        try:
            ak_quotes = fetch_akshare_quotes(all_a_codes, [])
        except:
            pass
        try:
            ak_etfs_data = fetch_akshare_etfs(etf_codes)
        except:
            pass
    else:
        print("[3/3] 跳过AKShare")

    # ===== 组装大盘指数 =====
    idx = sina_index.get("000001")
    if not idx:
        idx = fetch_akshare_index()  # fallback
    if idx:
        output["market"]["price"] = idx["price"]
        output["market"]["chg"] = idx["chg"]
        print(f"  大盘: {idx['price']} ({idx['chg']})")

    # ===== 组装A股各层 =====
    for layer_id, layer_info in HOLDINGS.items():
        stocks = layer_info["stocks"]
        if not stocks:
            output["layers"].append({
                "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                "color": layer_info["color"], "stocks": [], "avg": 0
            })
            continue

        layer_stocks = []
        total_chg = 0
        count = 0
        for s in stocks:
            code = s["code"]
            # 优先新浪价格，AKShare补充PE/市值
            sina_q = sina_all.get(code)
            ak_q = ak_quotes.get(code)
            if sina_q and sina_q["price"] != "-":
                cv = sina_q["cv"]
                item = {
                    "code": code, "name": s["name"], "sector": s["sector"],
                    "held": s.get("held", False), "pick": s.get("pick", False),
                    "p": sina_q["price"], "c": sina_q["chg"], "cv": cv,
                    "pe": ak_q["pe"] if ak_q else "-",
                    "cap": ak_q["cap"] if ak_q else "-",
                }
            elif ak_q and ak_q["price"] != "-":
                cv = ak_q["cv"]
                item = {
                    "code": code, "name": s["name"], "sector": s["sector"],
                    "held": s.get("held", False), "pick": s.get("pick", False),
                    "p": ak_q["price"], "c": ak_q["chg"], "cv": cv,
                    "pe": ak_q.get("pe", "-"), "cap": ak_q.get("cap", "-"),
                }
            else:
                cv = 0
                item = {
                    "code": code, "name": s["name"], "sector": s["sector"],
                    "held": s.get("held", False), "pick": s.get("pick", False),
                    "p": "-", "c": "0%", "cv": 0, "pe": "-", "cap": "-",
                }
            layer_stocks.append(item)
            total_chg += cv
            count += 1

        avg = round(total_chg / count, 2) if count else 0
        output["layers"].append({
            "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
            "color": layer_info["color"], "stocks": layer_stocks, "avg": avg
        })

    # ===== ETF =====
    for e in ETFS:
        code = e["code"]
        sina_q = sina_all.get(code)
        ak_q = ak_etfs_data.get(code)
        if sina_q and sina_q["price"] != "-":
            output["etfs"].append({
                "code": code, "name": e["name"], "sector": e["sector"],
                "p": sina_q["price"], "c": sina_q["chg"], "cv": sina_q["cv"],
            })
        elif ak_q:
            output["etfs"].append({
                "code": code, "name": e["name"], "sector": e["sector"],
                "p": ak_q["price"], "c": ak_q["chg"], "cv": ak_q["cv"],
            })
        else:
            output["etfs"].append({
                "code": code, "name": e["name"], "sector": e["sector"],
                "p": "-", "c": "0%", "cv": 0,
            })

    # ===== 港股 =====
    hk_result = []
    for s in HK_STOCKS:
        q = sina_hk.get(s["code"])
        if q and q["price"] != "-":
            hk_result.append({
                "code": s["code"], "name": s["name"], "sector": s["sector"],
                "held": s.get("held", False), "pick": s.get("pick", False),
                "p": q["price"], "c": q["chg"], "cv": q["cv"],
                "pe": "-", "cap": "-",
            })
        else:
            hk_result.append({
                "code": s["code"], "name": s["name"], "sector": s["sector"],
                "held": s.get("held", False), "pick": s.get("pick", False),
                "p": "-", "c": "0%", "cv": 0, "pe": "-", "cap": "-",
            })
    output["hk"] = hk_result

    # 同步港股到L5
    l5 = next((l for l in output["layers"] if l["id"] == "L5"), None)
    if l5 and hk_result:
        l5["stocks"] = hk_result
        vals = [s["cv"] for s in hk_result if s["cv"] != 0]
        l5["avg"] = round(sum(vals) / len(vals), 2) if vals else 0

    return output


def build_output_manual(skip_a=False, skip_hk=False):
    """MANUAL模式: 妙想(问财)为A股主数据源 + 富途OpenD仅港股
    数据源策略:
      A股/指数/ETF: 妙想API(问财数据，含PE/市值) → 新浪HTTP(备选价格) → 继承
      港股: 富途OpenD → 继承
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

    # ===== 第1步: 妙想API获取A股行情（含PE/市值） =====
    mx_all = {}  # name -> {price, chg, pe, cap}
    if not skip_a and MX_APIKEY:
        # 收集所有A股+ETF名称，分批查询（每批最多8个）
        all_a_items = []
        for layer_info in HOLDINGS.values():
            for s in layer_info["stocks"]:
                all_a_items.append(s)
        for e in ETFS:
            all_a_items.append(e)

        # 分批调用妙想API
        batch_size = 8
        for i in range(0, len(all_a_items), batch_size):
            batch = all_a_items[i:i + batch_size]
            names = [s["name"] for s in batch]
            codes = [s["code"] for s in batch]
            batch_num = i // batch_size + 1
            total_batches = (len(all_a_items) + batch_size - 1) // batch_size
            print(f"[1/4] 妙想API获取A股行情（批次 {batch_num}/{total_batches}）...")
            batch_quotes = fetch_a_stock_quotes(codes, names)
            mx_all.update(batch_quotes)
            if i + batch_size < len(all_a_items):
                time.sleep(0.5)  # 避免请求过快

        hit = sum(1 for v in mx_all.values() if v.get("price"))
        print(f"  妙想命中: {hit}/{len(all_a_items)}")
    elif not skip_a:
        print("[1/4] 妙想API未配置KEY，跳过")
    else:
        print("[1/4] 跳过A股（仅港股模式）")

    # ===== 第2步: 新浪HTTP补充缺失A股价格 =====
    sina_data = {}
    if not skip_a:
        # 收集妙想未命中的标的，用新浪补充
        all_a_codes = ["000001"]  # 上证综指
        for layer_info in HOLDINGS.values():
            for s in layer_info["stocks"]:
                all_a_codes.append(s["code"])
        for e in ETFS:
            all_a_codes.append(e["code"])

        print(f"[2/4] 新浪HTTP补充行情（{len(all_a_codes)}个标的）...")
        sina_data = fetch_sina_quotes(all_a_codes)
        sina_hit = sum(1 for v in sina_data.values() if v.get("price") and v["price"] != "-")
        print(f"  新浪命中: {sina_hit}/{len(all_a_codes)}")

    # ===== 第3步: 组装A股数据（妙想优先 → 新浪备选） =====
    def _mx_sina_to_display(item_cfg, is_etf=False):
        """将妙想+新浪数据组装为H5显示格式"""
        code = item_cfg["code"]
        name = item_cfg["name"]
        sector = item_cfg.get("sector", "")
        held = item_cfg.get("held", False)
        pick = item_cfg.get("pick", False)

        # 优先妙想数据（有PE/市值）
        mx = mx_all.get(name, {})
        sina = sina_data.get(code, {})

        price = mx.get("price") or sina.get("price", "-")
        pe = mx.get("pe") or sina.get("pe", "-")
        cap = mx.get("cap") or sina.get("cap", "-")

        # 涨跌幅: 优先妙想，备选新浪
        chg_str = mx.get("chg") or sina.get("chg", "0%")
        try:
            cv = float(str(chg_str).replace("%", "").replace("+", ""))
        except:
            cv = 0
        chg_display = f"+{cv}%" if cv > 0 else f"{cv}%"

        result = {
            "code": code, "name": name, "sector": sector,
            "p": str(price) if price else "-",
            "c": chg_display, "cv": cv,
        }
        if not is_etf:
            result["held"] = held
            result["pick"] = pick
            result["pe"] = str(pe) if pe and pe != "-" else "-"
            result["cap"] = str(cap) if cap and cap != "-" else "-"
        return result

    if not skip_a:
        print("[3/4] 组装A股 + ETF行情...")
        # 大盘指数（新浪）
        idx_sina = sina_data.get("000001", {})
        if idx_sina.get("price"):
            output["market"]["price"] = idx_sina["price"]
            output["market"]["chg"] = idx_sina.get("chg", "0%")
            print(f"  大盘: {idx_sina['price']} ({idx_sina.get('chg', '-')})")

        # 各层股票
        for layer_id, layer_info in HOLDINGS.items():
            stocks = layer_info["stocks"]
            if not stocks:
                output["layers"].append({
                    "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                    "color": layer_info["color"], "stocks": [], "avg": 0
                })
                continue

            layer_stocks = []
            total_chg = 0
            count = 0
            for s in stocks:
                item = _mx_sina_to_display(s)
                layer_stocks.append(item)
                total_chg += item["cv"]
                count += 1

            avg = round(total_chg / count, 2) if count else 0
            output["layers"].append({
                "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                "color": layer_info["color"], "stocks": layer_stocks, "avg": avg
            })

        # ETF行情
        for e in ETFS:
            item = _mx_sina_to_display(e, is_etf=True)
            output["etfs"].append({
                "code": item["code"], "name": item["name"], "sector": item["sector"],
                "p": item["p"], "c": item["c"], "cv": item["cv"]
            })
    else:
        print("[3/4] 跳过A股行情")
        for layer_id, layer_info in HOLDINGS.items():
            output["layers"].append({
                "id": layer_id, "name": layer_info["name"], "sub": layer_info["sub"],
                "color": layer_info["color"], "stocks": [], "avg": 0
            })

    # ===== 第4步: 港股（富途OpenD） =====
    hk_result = []
    if not skip_hk:
        futu_hk_codes = [s["futu"] for s in HK_STOCKS]
        print(f"[4/4] 富途OpenD获取港股（{len(futu_hk_codes)}个标的）...")
        snapshots = fetch_futu_snapshots(futu_hk_codes)
        futu_hk = {snap["code"]: snap for snap in snapshots}
        print(f"  成功获取: {len(futu_hk)}/{len(futu_hk_codes)}")

        for s in HK_STOCKS:
            snap = futu_hk.get(s["futu"])
            item = _snap_to_display(snap, s["code"], s["name"], s["sector"],
                                    s.get("held", False))
            hk_result.append(item)
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
    parser.add_argument("--manual", action="store_true", help="手动模式: 妙想(问财)+新浪+富途港股")
    parser.add_argument("--hk", action="store_true", help="仅刷新港股（手动模式）")
    parser.add_argument("--a", action="store_true", dest="a_only", help="仅刷新A股（手动模式）")
    parser.add_argument("--skip-a", action="store_true", help="自动模式下跳过A股（午休/港股延时段）")
    parser.add_argument("--skip-hk", action="store_true", help="自动模式下跳过港股")
    args = parser.parse_args()

    mode = "manual" if (args.manual or args.hk or args.a_only) else "auto"

    print(f"=== 投资工作室H5 数据刷新 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模式: {'🔧 手动（妙想+新浪+富途港股）' if mode == 'manual' else '⚡ 自动（新浪+AKShare）'}")
    print()

    if mode == "auto":
        data = build_output_auto(skip_a=args.skip_a, skip_hk=args.skip_hk)
    else:
        data = build_output_manual(skip_a=args.hk, skip_hk=args.a_only)

    # 继承上次有效数据：新数据为空时保留旧值
    if OUTPUT_FILE.exists():
        try:
            old = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            # 大盘指数
            if not data["market"].get("price") and old.get("market", {}).get("price"):
                data["market"] = old["market"]
                print("  [继承] 大盘指数沿用上次数据")
            # 层级summary继承
            old_layers = {l["id"]: l for l in old.get("layers", [])}
            for layer in data["layers"]:
                old_l = old_layers.get(layer["id"])
                if old_l:
                    for lk in ("summary", "summaryTime"):
                        if lk in old_l and lk not in layer:
                            layer[lk] = old_l[lk]
            # A股各层
            old_stocks = {}
            for layer in old.get("layers", []):
                for s in layer.get("stocks", []):
                    old_stocks[s["code"]] = s
            inherited = 0
            for layer in data["layers"]:
                for s in layer["stocks"]:
                    old_s = old_stocks.get(s["code"])
                    if old_s:
                        if s["p"] == "-" and old_s.get("p", "-") != "-":
                            s.update({k: old_s[k] for k in ("p", "c", "cv", "pe", "cap") if k in old_s})
                            inherited += 1
                        # 保留信号数据
                        for sk in ("signal", "signalNote", "signalTime", "confidence"):
                            if sk in old_s and sk not in s:
                                s[sk] = old_s[sk]
            # ETF
            old_etfs = {e["code"]: e for e in old.get("etfs", [])}
            for e in data["etfs"]:
                if e["p"] == "-" and e["code"] in old_etfs and old_etfs[e["code"]]["p"] != "-":
                    old_e = old_etfs[e["code"]]
                    e.update({k: old_e[k] for k in ("p", "c", "cv") if k in old_e})
                    inherited += 1
            # 港股
            old_hk = {s["code"]: s for s in old.get("hk", [])}
            for s in data["hk"]:
                if s["p"] == "-" and s["code"] in old_hk and old_hk[s["code"]]["p"] != "-":
                    old_s = old_hk[s["code"]]
                    s.update({k: old_s[k] for k in ("p", "c", "cv", "pe", "cap") if k in old_s})
                    inherited += 1
            # 同步港股到L5
            if inherited:
                l5 = next((l for l in data["layers"] if l["id"] == "L5"), None)
                if l5:
                    l5["stocks"] = data["hk"]
                    vals = [s["cv"] for s in data["hk"] if s["cv"] != 0]
                    l5["avg"] = round(sum(vals) / len(vals), 2) if vals else 0
                print(f"  [继承] {inherited}个标的沿用上次数据")
        except Exception as e:
            print(f"  [WARN] 读取旧数据失败: {e}")

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
