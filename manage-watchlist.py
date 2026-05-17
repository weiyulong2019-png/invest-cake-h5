#!/usr/bin/env python3
"""
社群自选股管理 — 增删查 + 行情刷新

用法:
  python3 manage-watchlist.py add "雅克科技" --code 002409 --sector "半导体材料" --by "小龙虾"
  python3 manage-watchlist.py add "腾讯控股" --code HK.00700 --sector "港股互联网" --by "老王"
  python3 manage-watchlist.py remove 002409
  python3 manage-watchlist.py list
  python3 manage-watchlist.py refresh          # 刷新所有自选股行情

数据存储: watchlist.json（同目录）
行情来源: 新浪财经HTTP（A股+港股）
"""

import os
import sys
import json
import argparse
from datetime import datetime
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

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"


def load_watchlist():
    if WATCHLIST_FILE.exists():
        return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    return {"updateTime": "", "stocks": []}


def save_watchlist(data):
    data["updateTime"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detect_type(code):
    """根据代码判断类型"""
    if code.startswith("HK."):
        return "hk"
    if code.startswith("51") or code.startswith("15") or code.startswith("16"):
        # ETF常见前缀: 510/512/515/516/159/160
        if len(code) == 6:
            prefix = code[:3]
            if prefix in ("510", "511", "512", "513", "515", "516", "517", "518", "159", "160", "161"):
                return "etf"
    return "a"


def cmd_add(args):
    data = load_watchlist()
    # 检查重复
    existing = [s for s in data["stocks"] if s["code"] == args.code]
    if existing:
        print(f"⚠️  {args.code} ({existing[0]['name']}) 已在自选中（由{existing[0]['addedBy']}添加）")
        return

    stock_type = detect_type(args.code)
    entry = {
        "code": args.code,
        "name": args.name,
        "sector": args.sector or "",
        "type": stock_type,
        "addedBy": args.by or "OpenClaw",
        "addedAt": datetime.now().strftime("%Y-%m-%d"),
        "p": "-", "c": "0%", "cv": 0
    }
    data["stocks"].append(entry)
    save_watchlist(data)
    print(f"✅ 已添加: {args.name}({args.code}) [{stock_type}] by {entry['addedBy']}")
    print(f"   当前自选: {len(data['stocks'])}只")


def cmd_remove(args):
    data = load_watchlist()
    before = len(data["stocks"])
    removed = [s for s in data["stocks"] if s["code"] == args.code]
    data["stocks"] = [s for s in data["stocks"] if s["code"] != args.code]
    after = len(data["stocks"])
    if before == after:
        print(f"⚠️  未找到代码: {args.code}")
    else:
        save_watchlist(data)
        print(f"✅ 已删除: {removed[0]['name']}({args.code})")
        print(f"   剩余自选: {after}只")


def cmd_list(args):
    data = load_watchlist()
    if not data["stocks"]:
        print("📭 自选列表为空")
        return
    print(f"📋 社群自选股（共{len(data['stocks'])}只）")
    print(f"   更新时间: {data['updateTime']}")
    print()
    for s in data["stocks"]:
        price = s.get("p", "-")
        chg = s.get("c", "0%")
        print(f"  {s['name']:10s} {s['code']:12s} [{s['type']:3s}] {price:>10s} {chg:>8s}  by {s.get('addedBy','?')}")


def cmd_refresh(args):
    """刷新所有自选股行情（新浪HTTP）"""
    data = load_watchlist()
    if not data["stocks"]:
        print("📭 自选列表为空，无需刷新")
        return

    # 分离A股/ETF和港股
    a_codes = [s["code"] for s in data["stocks"] if s["type"] in ("a", "etf")]
    hk_codes = [s["code"] for s in data["stocks"] if s["type"] == "hk"]

    quotes = {}

    # A股+ETF: 新浪
    if a_codes:
        sina_codes = []
        for code in a_codes:
            if code.startswith("6"):
                sina_codes.append(f"sh{code}")
            else:
                sina_codes.append(f"sz{code}")
        try:
            url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
            headers = {"Referer": "http://finance.sina.com.cn"}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                if "=" not in line:
                    continue
                var_part, data_part = line.split("=", 1)
                sina_code = var_part.split("_")[-1]
                raw_code = sina_code[2:]  # 去掉sh/sz前缀
                fields = data_part.strip('";\n').split(",")
                if len(fields) < 4:
                    continue
                try:
                    current = float(fields[3])
                    prev_close = float(fields[2])
                    if current > 0 and prev_close > 0:
                        cv = round((current - prev_close) / prev_close * 100, 2)
                        chg = f"+{cv}%" if cv > 0 else f"{cv}%"
                        quotes[raw_code] = {"p": str(round(current, 3)), "c": chg, "cv": cv}
                except (ValueError, IndexError):
                    pass
            print(f"[新浪] A股+ETF: {len(quotes)}/{len(a_codes)} 命中")
        except Exception as e:
            print(f"[WARN] 新浪A股查询失败: {e}")

    # 港股: 新浪
    if hk_codes:
        hk_sina = []
        for code in hk_codes:
            num = code.replace("HK.", "")
            hk_sina.append(f"rt_hk{num}")
        try:
            url = f"http://hq.sinajs.cn/list={','.join(hk_sina)}"
            headers = {"Referer": "http://finance.sina.com.cn"}
            resp = requests.get(url, headers=headers, timeout=10)
            resp.encoding = "gbk"
            hk_hit = 0
            for line in resp.text.strip().split("\n"):
                if "=" not in line:
                    continue
                var_part, data_part = line.split("=", 1)
                sina_code = var_part.split("_")[-1]
                num = sina_code.replace("rt_hk", "")
                hk_code = f"HK.{num}"
                fields = data_part.strip('";\n').split(",")
                if len(fields) < 7:
                    continue
                try:
                    current = float(fields[6])
                    prev_close = float(fields[3])
                    if current > 0 and prev_close > 0:
                        cv = round((current - prev_close) / prev_close * 100, 2)
                        chg = f"+{cv}%" if cv > 0 else f"{cv}%"
                        quotes[hk_code] = {"p": str(round(current, 3)), "c": chg, "cv": cv}
                        hk_hit += 1
                except (ValueError, IndexError):
                    pass
            print(f"[新浪] 港股: {hk_hit}/{len(hk_codes)} 命中")
        except Exception as e:
            print(f"[WARN] 新浪港股查询失败: {e}")

    # 写回
    updated = 0
    for s in data["stocks"]:
        q = quotes.get(s["code"])
        if q:
            s["p"] = q["p"]
            s["c"] = q["c"]
            s["cv"] = q["cv"]
            updated += 1

    save_watchlist(data)
    print(f"✅ 已刷新 {updated}/{len(data['stocks'])} 只自选股行情")


def main():
    parser = argparse.ArgumentParser(description="社群自选股管理")
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="添加自选股")
    p_add.add_argument("name", help="股票名称")
    p_add.add_argument("--code", required=True, help="股票代码（如002409, HK.01810）")
    p_add.add_argument("--sector", default="", help="板块")
    p_add.add_argument("--by", default="OpenClaw", help="添加人")

    p_rm = sub.add_parser("remove", help="删除自选股")
    p_rm.add_argument("code", help="股票代码")

    sub.add_parser("list", help="列出所有自选股")
    sub.add_parser("refresh", help="刷新行情")

    args = parser.parse_args()
    if args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "remove":
        cmd_remove(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "refresh":
        cmd_refresh(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
