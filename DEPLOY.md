# 投资小龙虾 H5 — 部署指南

## 快速开始（本地预览）

```bash
cd ~/.openclaw/投资工作室H5

# 1. 刷新行情数据
export MX_APIKEY=mkt_f-JSym1MjVyEBaoal60UkgLwEd69FhteaSCakjQE8Ic
python3 refresh-data.py

# 2. 启动本地服务
python3 -m http.server 8080
# 打开 http://localhost:8080
```

---

## 部署到线上（微信可访问）

### 方案A：Cloudflare Pages（推荐，免费+国内快）

**一次性配置：**

1. 在 GitHub 创建仓库 `invest-cake-h5`
2. 推送代码：
   ```bash
   cd ~/.openclaw/投资工作室H5
   git init && git add .
   git commit -m "init: 投资工作室H5"
   git remote add origin git@github.com:你的用户名/invest-cake-h5.git
   git push -u origin main
   ```

3. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com) → Pages → Create Project
   - 连接 GitHub 仓库 `invest-cake-h5`
   - Build command: 留空
   - Output directory: `.` (根目录)
   - 点击 Deploy

4. 获取域名: `invest-cake.pages.dev`（国内可直连，无需备案）

5. 配置 GitHub Secrets（自动刷新行情）：
   - `MX_APIKEY`: 妙想API Key
   - `CF_API_TOKEN`: Cloudflare API Token（Pages部署权限）
   - `CF_ACCOUNT_ID`: Cloudflare Account ID

**部署后效果：**
- 交易日 9:30-15:00 每30分钟自动刷新行情
- 直接在微信聊天里发链接即可打开
- 支持微信内分享卡片（需额外配置JS-SDK签名后端）

---

### 方案B：腾讯云COS静态托管（国内备案域名）

适合需要自定义域名 + 微信JS-SDK完整功能的场景：

```bash
# 安装腾讯云CLI
pip install coscmd

# 配置
coscmd config -a <SecretId> -s <SecretKey> -b invest-h5-<appid> -r ap-shanghai

# 上传
coscmd upload -r . /

# 绑定域名 + 开启CDN + 申请SSL证书
```

---

### 方案C：Vercel（备选）

```bash
npm i -g vercel
cd ~/.openclaw/投资工作室H5
vercel --prod
```

---

## 微信分享卡片配置

完整的微信分享需要后端签名。简易方案：

1. 申请微信公众号（服务号）
2. 在公众号设置 → JS安全域名 → 添加你的域名
3. 部署一个签名接口（可用 Cloudflare Workers）：

```javascript
// workers/wx-sign.js — Cloudflare Worker
export default {
  async fetch(request) {
    const url = new URL(request.url).searchParams.get('url');
    // 获取 access_token → jsapi_ticket → 签名
    // 参考: https://developers.weixin.qq.com/doc/offiaccount/OA_Web_Apps/JS-SDK.html
    return new Response(JSON.stringify({ signature, timestamp, nonceStr }));
  }
}
```

4. H5 初始化时请求签名接口并调用 `wx.config()`

> 注：不配置JS-SDK也能在微信中打开H5，只是分享时无法自定义卡片标题和缩略图。

---

## 数据源说明

| 数据 | 来源 | 刷新频率 |
|------|------|---------|
| A股行情 | 妙想API（东方财富） | 交易日每30分钟 |
| 港股行情 | 富途OpenD（待接入） | 静态占位 |
| AI洞察 | OpenClaw汇总Agent | 手动/心跳 |

---

## 目录结构

```
投资工作室H5/
├── index.html           # H5主页面（Vue 3单文件）
├── data.json            # 行情数据（自动生成）
├── refresh-data.py      # 数据刷新脚本
├── share-thumb.svg      # 分享缩略图
├── package.json         # 项目配置
├── _headers             # Cloudflare响应头
├── _redirects           # 路由规则
├── .gitignore
├── DEPLOY.md            # 本文件
└── .github/workflows/
    └── refresh-and-deploy.yml  # CI/CD
```

---

## FAQ

**Q: 微信里打开白屏？**
A: 检查是否用了ES6+语法（已兼容），确认HTTPS证书正常。

**Q: 数据没刷新？**
A: 检查 GitHub Actions 是否运行成功，或手动执行 `python3 refresh-data.py`。

**Q: 港股数据怎么接入？**
A: 在 `refresh-data.py` 中取消港股静态占位，接入富途OpenD API（需本地OpenD运行）。
后续方案：部署一个轻量级中转服务，从富途获取港股数据写入data.json。
