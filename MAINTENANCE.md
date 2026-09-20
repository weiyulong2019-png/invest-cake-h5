# 本机维护手册（Windows）

站点：https://invest-cake.pages.dev ｜ 仓库：github.com/weiyulong2019-png/invest-cake-h5
发布链路：**本地改文件 → git push main → GitHub Actions → Cloudflare Pages 自动部署**（无构建步骤）。

## 1. 本机位置与环境

| 项 | 值 |
|---|---|
| 仓库目录 | `D:\workbuddy\invest-cake-h5` |
| Python | `C:\Users\win\.workbuddy\binaries\python\envs\invest-cake\Scripts\python.exe`（3.13，已装 requests + akshare） |
| 维护脚本 | `tools\windows\*.ps1` |
| 日志 | `logs\`（git 已忽略） |
| 秘钥 | `.env.local`（git 已忽略，用 `.env.local.example` 建） |

## 2. 日常命令（PowerShell，在仓库根目录）

```powershell
powershell -File tools\windows\serve.ps1              # 本地预览 http://127.0.0.1:8080
powershell -File tools\windows\refresh.ps1            # 刷行情（新浪+AKShare），写 data.json
powershell -File tools\windows\refresh.ps1 --manual   # 手动模式，需要 MX_APIKEY
powershell -File tools\windows\signals.ps1            # 生成信号，写 data.json
powershell -File tools\windows\brief.ps1              # 收盘简报，写 daily_brief.json
powershell -File tools\windows\publish.ps1            # 校验 + commit + push（触发部署）
```

页面是纯静态：`index.html` + `data.json` / `strategy.json` / `daily_brief.json` / `watchlist.json`，改完刷新浏览器即可见（脚本带了 `?t=` 时间戳绕过缓存）。

## 3. 定时刷新（替代 macOS launchd）

```powershell
powershell -File tools\windows\install-scheduled-task.ps1          # 注册：工作日每 5 分钟
powershell -File tools\windows\install-scheduled-task.ps1 -Uninstall
```

`scheduled-refresh.ps1` 内部按时间决定动作，与原 `auto-refresh.sh` 一致：

| 时间 | 动作 |
|---|---|
| 05:55–06:10 | 盘前信号（每日一次） |
| 09:25 / :00 / :30（含 13:00–15:15） | 行情 + 信号 |
| 每 5 分钟（09:30–15:15） | 仅行情 |
| 11:31–12:59、15:16–16:15 | 仅港股 |
| 周末、16:15 后 | 跳过 |

刷新后自动调用 `publish.ps1`（先跑 `validate-public-data.py --scope market` 防呆，再 push）。不想自动推送就加 `-NoPublish`。

## 4. 发布

1. 改文件 → 本地预览确认。
2. `publish.ps1`（或手动 `git add/commit/push`）。
3. GitHub Actions `deploy.yml` 用 wrangler 把根目录推到 Cloudflare Pages，约 1 分钟生效。
4. 首次 push 会弹 Git Credential Manager 窗口做 GitHub 授权，授权一次后缓存。

## 5. 数据源优先级（2026-09-21 起）

行情走**扶摇（同花顺）优先，缺失自动降级**，实现在 `fuyao_source.py`（key 从 `FUYAO_API_KEY` 读）：

| 数据 | 第一优先级 | 兜底 | 说明 |
|---|---|---|---|
| A股价格 | 扶摇 `/api/a-share/prices/snapshot` | 新浪 → 腾讯 → AKShare | 批量；单个坏码会整批失败，已做逐只重试 |
| A股 PE | 扶摇 `/api/a-share/valuations/snapshot` | AKShare | 扶摇给 `pe_ttm`，AKShare 常连不上东财 |
| ETF | 扶摇 `/api/fund/market/snapshot` | 新浪 → AKShare | 只支持单只查询；非交易时段返回 3002「未就绪」→ 自动走兜底 |
| 指数 | 扶摇 `/api/a-share-index/prices/snapshot` | 新浪 → AKShare | 上证 `000001.SH` |
| 市值 `cap` | AKShare | — | **扶摇无市值字段**，目前仍依赖 AKShare，东财不通时为 `-` |
| 港股 | 新浪 → 腾讯 | 富途 OpenD（需本机运行） | **扶摇不支持港股** |

代码映射：6/5 开头 → `.SH`，其余 → `.SZ`；ETF/LOF（1/5 开头）走基金接口。

## 6. 注意事项

- **行尾**：仓库含 `.sh` 脚本，建议 `git config core.autocrlf input`，避免 CRLF 提交。
- **不要提交**：`.env.local`、`logs\`、`data.json.bak` 等（已在 .gitignore）。
- **PowerShell 中文**：控制台默认 GBK，`git log` 中文会显示乱码。脚本输出统一落 `logs\*.log`（UTF-8），用编辑器看。
- **代理**：脚本内已强制 `requests` 不走系统代理；若公司网络需要代理，先确认能直连 `hq.sinajs.cn`。

## 7. 待处理

- [x] ~~`DEPLOY.md` 明文 `MX_APIKEY`~~ → 已清除，仍需**去妙想后台轮换 key**（历史 commit 里还在）。
- [ ] 市值 `cap` 仍缺：扶摇无市值字段，AKShare 连不上东财时为 `-`。可选方案：每日采集扶摇竞价接口 `float_market_cap` 反推。
- [ ] 港股实时仍走新浪/富途，扶摇不支持。
- [ ] `generate-signals.py` 的历史 K 线仍用 AKShare（东财常失败 → 信号降级为 quote-fallback），可改用扶摇 `/api/a-share/prices/historical`（单只、仅日线）自建指标。
- [ ] Windows 计划任务（工作日每 5 分钟刷新并自动 push）尚未注册。
