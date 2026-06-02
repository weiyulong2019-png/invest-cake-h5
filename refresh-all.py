#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh-all.py — 投资工作室H5「每日本机刷新」总编排器（公开看板专用）

每天早上按序跑【能喂 H5 公开看板】的数据生成器，做公开安全自检，再 git push。
本脚本只负责【编排 + 安全闸 + 提交】，不改任何业务/调度配置，不实盘，不读打印密钥。

═══════════════════════════════════════════════════════════════════════════
 cron / 调度规格（请由运维另行登记，本脚本绝不自行修改 cron / launchd）：
   每天 07:00 Asia/Shanghai 执行：
     0 7 * * *  cd /Users/long/.openclaw/投资工作室H5 && /usr/bin/python3 refresh-all.py >> refresh.log 2>&1
   ~/.openclaw/cron/jobs.json 由其所有者维护；这里只给规格，不手改。
═══════════════════════════════════════════════════════════════════════════

执行顺序（每步独立 try/except，失败/缺失则跳过不中断）：
  1. refresh-data.py        行情 / ETF / 港股（H5 主数据 data.json）
  2. us_intel/sec_edgar.py  美股 SEC filings + Form4 内部人（逐关注标的）
  3. us_intel/us_quote.py   美股行情（多源 fallback）
  4. rss_intel.py           RSS 实时情报（→ data/intel/rss_latest.json）
  5. polymarket_odds.py     Polymarket 事件赔率（→ data/intel/polymarket_latest.json）
  6. strategy-feed.py       策略看板（折叠美股情报 + intel 块 → strategy.json）
  7. daily-brief.py         每日简报（daily_brief.json）

安全闸（铁律）：
  • 提交前对【将要提交的公开文件】做泄露自检：命中个人持仓量 / 成本 / 盈亏 /
    密钥 → abort，绝不 commit / push。
  • 只 add 白名单安全文件；commit 用固定英文 message（规避 Cloudflare 非 ASCII 报错）。
  • 引擎产出的 scripts/data/us_intel/*.json（含逐股 shares 明细）**不在** H5 仓库内，
    不会被提交；自检只扫 H5 仓库内的公开文件。

用法：
  python3 refresh-all.py              # 全流程（best-effort + 自检 + push）
  python3 refresh-all.py --no-push    # 跑数据 + 自检 + commit，但不 push
  python3 refresh-all.py --no-git     # 只跑数据生成，完全不碰 git
  python3 refresh-all.py --dry-run    # 演练：打印将执行的步骤，不联网不写不提交
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE_SCRIPTS = Path("/Users/long/.openclaw/workspace-main/scripts")
US_INTEL_DIR = WORKSPACE_SCRIPTS / "us_intel"

# 关注的美股锚标的（与 strategy-feed/us_ash_anchor 对齐；逐个拉 SEC 内部人）。
# 为控制 SEC 礼貌频率，默认取核心若干；可按需扩。
US_TICKERS = ["NVDA", "TSM", "MU", "AVGO", "MSFT", "AMD", "AAPL",
              "GOOGL", "META", "AMZN", "TSLA", "AMAT", "ARM", "SMCI"]

# git 只提交这些【公开安全】文件（看板 + 生成器；绝不含引擎数据/密钥）。
SAFE_COMMIT_FILES = [
    "index.html",
    "strategy.json",
    "daily_brief.json",
    "data.json",
    "strategy-feed.py",
    "daily-brief.py",
    "refresh-data.py",
    "refresh-all.py",
]

FIXED_COMMIT_MSG = "chore: daily auto-refresh public dashboard data"

PY = sys.executable or "python3"


# ───────────────────────── 运行单步（容错） ─────────────────────────
def run_step(label: str, cmd: list[str], cwd: Path, dry_run: bool) -> dict:
    """跑一个子命令；任何失败都被捕获，不中断整体编排。"""
    printable = " ".join(str(c) for c in cmd)
    if dry_run:
        print(f"  [dry-run] {label}: would run `{printable}` (cwd={cwd})")
        return {"label": label, "status": "dry-run"}
    # 兜底：若脚本路径不存在则优雅跳过（各调用方通常已先 .exists() 过滤）。
    if len(cmd) > 1 and cmd[0] == PY and not Path(cmd[1]).exists():
        print(f"  [SKIP] {label}: 脚本不存在 {cmd[1]}")
        return {"label": label, "status": "skip", "reason": "missing script"}
    print(f"  [RUN ] {label}: {printable}")
    env = dict(os.environ)
    # 绕过沙箱代理（与 auto-refresh.sh 一致）；外网仍可能被拦→子脚本自行降级。
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    try:
        r = subprocess.run(cmd, cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0
        tail = (r.stdout or "").strip().splitlines()[-2:]
        print(f"         -> {'OK' if ok else 'FAIL rc=' + str(r.returncode)}"
              + (f" | {' / '.join(tail)}" if tail else ""))
        if not ok and r.stderr:
            print(f"         stderr: {r.stderr.strip().splitlines()[-1:]}")
        return {"label": label, "status": "ok" if ok else "fail",
                "rc": r.returncode}
    except subprocess.TimeoutExpired:
        print(f"         -> TIMEOUT (>600s), 跳过")
        return {"label": label, "status": "timeout"}
    except FileNotFoundError as e:
        print(f"         -> SKIP (not found): {e}")
        return {"label": label, "status": "skip", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        print(f"         -> ERROR: {e}")
        return {"label": label, "status": "error", "reason": str(e)}


# ───────────────────────── 数据生成阶段 ─────────────────────────
def generate_all(dry_run: bool) -> list[dict]:
    results = []

    # 1. H5 主数据（行情/ETF/港股）
    rd = HERE / "refresh-data.py"
    if rd.exists():
        results.append(run_step("refresh-data", [PY, str(rd)], HERE, dry_run))
    else:
        print("  [SKIP] refresh-data: 脚本不存在")

    # 2. 美股 SEC filings + Form4 内部人（逐标的）
    sec = US_INTEL_DIR / "sec_edgar.py"
    if sec.exists():
        ok = 0
        for tk in US_TICKERS:
            r = run_step(f"sec_edgar:{tk}",
                         [PY, str(sec), "all", tk, "--limit", "6"],
                         US_INTEL_DIR, dry_run)
            if r.get("status") in ("ok", "dry-run"):
                ok += 1
        results.append({"label": "sec_edgar(batch)", "status": "ok" if ok else "fail",
                        "ok_count": ok, "total": len(US_TICKERS)})
    else:
        print("  [SKIP] sec_edgar: 模块未就绪")
        results.append({"label": "sec_edgar", "status": "skip"})

    # 3. 美股行情（多源 fallback，多 ticker 一次）
    usq = US_INTEL_DIR / "us_quote.py"
    if usq.exists():
        results.append(run_step("us_quote",
                                [PY, str(usq), *US_TICKERS, "--days", "0"],
                                US_INTEL_DIR, dry_run))
    else:
        print("  [SKIP] us_quote: 模块未就绪")
        results.append({"label": "us_quote", "status": "skip"})

    # 4. RSS 情报
    rss = WORKSPACE_SCRIPTS / "rss_intel.py"
    if rss.exists():
        results.append(run_step("rss_intel", [PY, str(rss), "--limit", "6"],
                                WORKSPACE_SCRIPTS, dry_run))
    else:
        print("  [SKIP] rss_intel: 模块未就绪")
        results.append({"label": "rss_intel", "status": "skip"})

    # 5. Polymarket 赔率
    poly = WORKSPACE_SCRIPTS / "polymarket_odds.py"
    if poly.exists():
        results.append(run_step("polymarket_odds", [PY, str(poly)],
                                WORKSPACE_SCRIPTS, dry_run))
    else:
        print("  [SKIP] polymarket_odds: 模块未就绪")
        results.append({"label": "polymarket_odds", "status": "skip"})

    # 6. 策略看板（折叠美股情报 + intel 块）—— 必须在上面情报数据之后
    sf = HERE / "strategy-feed.py"
    if sf.exists():
        results.append(run_step("strategy-feed", [PY, str(sf)], HERE, dry_run))
    else:
        print("  [SKIP] strategy-feed: 脚本不存在")

    # 7. 每日简报
    db = HERE / "daily-brief.py"
    if db.exists():
        results.append(run_step("daily-brief", [PY, str(db)], HERE, dry_run))
    else:
        print("  [SKIP] daily-brief: 脚本不存在")

    return results


# ───────────────────────── 公开安全自检（提交前闸门） ─────────────────────────
# 设计要点：
#   • 数据文件(.json)：会被前端原样展示，必须严查【个人头寸/损益的键与中文实义词】。
#   • 源码(.py/.html)：注释/UI 文案/本自检规则本身合法地提到"持仓量""shares"键名
#     （如免责声明"公开看板不含持仓量"），对它们只查【真·密钥/令牌】，
#     不因描述性文字误伤而 abort。
#   • 密钥模式带赋值上下文，避免误伤 RSS 新闻里的 tokenisation / shared 等普通词。

# 个人头寸/损益 —— 仅对数据文件生效
DATA_LEAK_PATTERNS = [
    (r'"(shares|share_count|position_size|qty|quantity|cost|cost_basis|'
     r'avg_cost|pnl|profit_loss|unrealized|realized_pnl|holdings_qty|持仓量|'
     r'持仓数|持股数|成本)"\s*:', "个人头寸/损益键"),
    (r'(持仓量|持仓数|持股数|持股量|成本价|建仓价|浮盈|浮亏|盈亏金额)', "中文持仓/盈亏词"),
]

# 密钥/令牌 —— 对所有文件生效（含源码）
SECRET_PATTERNS = [
    (r'(api[_-]?key|secret[_-]?key|access[_-]?token|private[_-]?key|'
     r'client[_-]?secret)\s*[:=]\s*["\']?[A-Za-z0-9_\-]{16,}', "密钥赋值"),
    (r'\bBearer\s+[A-Za-z0-9_\-\.]{16,}', "Bearer 令牌"),
    (r'sk-[A-Za-z0-9]{16,}', "sk- 风格密钥"),
    (r'gh[pousr]_[A-Za-z0-9]{20,}', "GitHub token"),
]


def safety_audit(files: list[str]) -> tuple[bool, list[str]]:
    """扫描【将提交的公开文件】是否泄露个人持仓/密钥。返回 (是否安全, 命中明细)。

    .json → 数据泄露 + 密钥 全查；.py/.html → 仅查密钥（文案/注释合法提及持仓词）。
    """
    hits: list[str] = []
    secret_rgx = [(re.compile(p, re.IGNORECASE), why) for p, why in SECRET_PATTERNS]
    data_rgx = [(re.compile(p, re.IGNORECASE), why) for p, why in DATA_LEAK_PATTERNS]
    for fname in files:
        fp = HERE / fname
        if not fp.exists():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            hits.append(f"{fname}: 读取失败({e}) — 保守起见标记命中")
            continue
        rules = list(secret_rgx)
        if fname.lower().endswith(".json"):
            rules = data_rgx + secret_rgx
        for rgx, why in rules:
            m = rgx.search(text)
            if m:
                snippet = m.group(0)[:60].replace("\n", " ")
                hits.append(f"{fname}: [{why}] 命中 `{snippet}`")
    return (len(hits) == 0, hits)


# ───────────────────────── git 提交 / 推送 ─────────────────────────
def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(HERE),
                          capture_output=True, text=True, timeout=120)


def git_commit_push(no_push: bool, dry_run: bool) -> dict:
    # 只 add 存在的白名单文件
    present = [f for f in SAFE_COMMIT_FILES if (HERE / f).exists()]
    if dry_run:
        print(f"  [dry-run] would: git add {present}; "
              f"git commit -m '{FIXED_COMMIT_MSG}'"
              + ("" if no_push else "; git push"))
        return {"status": "dry-run"}

    add = _git(["add", "--", *present])
    if add.returncode != 0:
        print(f"  [git] add 失败: {add.stderr.strip()}")
        return {"status": "add_failed"}

    # 是否有变更可提交
    status = _git(["status", "--porcelain", "--", *present])
    if not status.stdout.strip():
        print("  [git] 无变更，跳过 commit/push")
        return {"status": "nothing_to_commit"}

    commit = _git(["commit", "-m", FIXED_COMMIT_MSG])
    if commit.returncode != 0:
        print(f"  [git] commit 失败: {commit.stderr.strip() or commit.stdout.strip()}")
        return {"status": "commit_failed"}
    print(f"  [git] committed: {FIXED_COMMIT_MSG}")

    if no_push:
        print("  [git] --no-push, 不推送")
        return {"status": "committed_no_push"}

    push = _git(["push"])
    if push.returncode != 0:
        print(f"  [git] push 失败（不致命，下次重试）: {push.stderr.strip()}")
        return {"status": "push_failed", "stderr": push.stderr.strip()[:200]}
    print("  [git] pushed OK")
    return {"status": "pushed"}


# ───────────────────────── 主流程 ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="投资工作室H5 每日刷新总编排（公开看板）")
    ap.add_argument("--dry-run", action="store_true", help="演练：不联网/不写/不提交")
    ap.add_argument("--no-git", action="store_true", help="只跑数据生成，不碰 git")
    ap.add_argument("--no-push", action="store_true", help="commit 但不 push")
    args = ap.parse_args()

    t0 = datetime.now()
    print("=" * 64)
    print(f"=== 投资工作室H5 每日刷新编排  {t0:%Y-%m-%d %H:%M:%S} "
          f"{'[DRY-RUN]' if args.dry_run else ''}")
    print(f"=== H5 目录: {HERE}")
    print(f"=== 引擎目录: {WORKSPACE_SCRIPTS} "
          f"({'可达' if WORKSPACE_SCRIPTS.is_dir() else '不可达(美股/RSS情报将跳过)'})")
    print("=" * 64)

    print("\n[阶段1] 数据生成 ...")
    gen_results = generate_all(args.dry_run)

    print("\n[阶段2] 公开安全自检（提交前闸门）...")
    safe, hits = safety_audit(SAFE_COMMIT_FILES)
    if not safe:
        print("  🔴 安全自检命中以下疑似泄露，ABORT（不 commit / 不 push）:")
        for h in hits:
            print(f"     - {h}")
        print("\n=== 已中止：请人工核查后再提交。===")
        return 2
    print("  ✅ 安全自检通过：未发现个人持仓量/成本/盈亏/密钥泄露。")

    print("\n[阶段3] git 提交 / 推送 ...")
    if args.no_git:
        print("  [SKIP] --no-git, 不碰 git")
        git_result = {"status": "skipped_no_git"}
    else:
        git_result = git_commit_push(args.no_push, args.dry_run)

    dt = (datetime.now() - t0).total_seconds()
    print("\n" + "=" * 64)
    ok = sum(1 for r in gen_results if r.get("status") in ("ok", "dry-run"))
    print(f"=== 完成  用时 {dt:.0f}s · 数据步骤 OK {ok}/{len(gen_results)} · "
          f"安全自检 通过 · git {git_result.get('status')}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
