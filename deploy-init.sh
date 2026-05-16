#!/bin/bash
# 投资工作室H5 — 一键初始化部署脚本
# 用法: chmod +x deploy-init.sh && ./deploy-init.sh

set -e

REPO_NAME="invest-cake-h5"
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "🚀 投资工作室H5 部署初始化"
echo "=========================="
echo ""

# 1. 初始化Git
if [ ! -d ".git" ]; then
    echo "[1/4] 初始化 Git 仓库..."
    git init
    git add .
    git commit -m "🎂 init: AI五层蛋糕H5"
else
    echo "[1/4] Git 已初始化，跳过"
fi

# 2. 创建GitHub仓库（需要gh CLI）
if command -v gh &>/dev/null; then
    echo "[2/4] 创建 GitHub 仓库..."
    if gh repo view "$REPO_NAME" &>/dev/null 2>&1; then
        echo "  仓库已存在，跳过创建"
    else
        gh repo create "$REPO_NAME" --private --source=. --push
        echo "  ✅ 仓库创建成功: https://github.com/$(gh api user -q .login)/$REPO_NAME"
    fi
else
    echo "[2/4] ⚠️  未安装 gh CLI，请手动创建仓库:"
    echo "  → https://github.com/new (名称: $REPO_NAME, Private)"
    echo "  然后执行:"
    echo "    git remote add origin git@github.com:你的用户名/$REPO_NAME.git"
    echo "    git push -u origin main"
    echo ""
    read -p "  完成后按回车继续..."
fi

# 3. 确认remote并推送
if git remote get-url origin &>/dev/null 2>&1; then
    echo "[3/4] 推送到 GitHub..."
    git push -u origin main 2>/dev/null || git push -u origin master
    echo "  ✅ 代码已推送"
else
    echo "[3/4] ⚠️  请先添加 remote origin"
fi

# 4. 提示Cloudflare配置
echo ""
echo "[4/4] 下一步: 配置 Cloudflare Pages"
echo "======================================"
echo ""
echo "  1. 打开 https://dash.cloudflare.com → Pages → Create a project"
echo "  2. 选择 'Connect to Git' → 授权 GitHub → 选择 $REPO_NAME"
echo "  3. 配置:"
echo "     - Project name: invest-cake"
echo "     - Build command: (留空)"
echo "     - Build output directory: ."
echo "  4. 点击 'Save and Deploy'"
echo ""
echo "  部署完成后你会得到: https://invest-cake.pages.dev"
echo ""
echo "  5. 配置 GitHub Secrets (Settings → Secrets → Actions):"
echo "     - MX_APIKEY = mkt_f-JSym1MjVyEBaoal60UkgLwEd69FhteaSCakjQE8Ic"
echo "     - CF_API_TOKEN = (Cloudflare API Token, 需Pages部署权限)"
echo "     - CF_ACCOUNT_ID = (Cloudflare仪表板右侧栏可见)"
echo ""
echo "🎉 完成后，交易日每30分钟自动刷新行情数据并部署！"
echo "   微信里直接发 https://invest-cake.pages.dev 即可打开"
