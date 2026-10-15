# 抖音续火花助手（Douyin Spark Keeper）

一个**自托管**的抖音"续火花"自动化工具：部署在你自己的服务器上，每天定时自动给指定好友发送一条私信，维持聊天火花（🔥）不熄灭。

支持**单份代码上传多个账号的登录态文件**，每个账号独立配置好友名单、发送时间偏移与好友间隔；系统按账号分时触发，浏览器任务之间严格串行，避免同时跑多个 Chromium 撑爆内存。

提供网页管理界面，日常**完全无人值守**——只需在登录态过期时重新扫码上传一次。基于 **Python + FastAPI + Playwright** 实现，支持 Debian/Ubuntu 一键部署，可选 nginx + HTTPS 域名访问。

> ⚠️ 仅限本人账号、少量好友、每天一条的**个人自用**场景。自动化发私信违反抖音社区公约，存在被风控、限流甚至封号的风险，使用后果自负。请勿用于批量营销、多账号运营或对外提供服务。

## 功能特性

- **多账号上传与独立配置**：一次上传多个账号的 `state.json` 登录态，每个账号有独立的好友名单、文案模板、发送时间偏移与好友间隔，互不干扰
- **分时发送**：每个账号在基准发送时间上叠加自己的偏移（如 21:00、21:20、21:40），分批次发送，避免同时触发抖音风控
- **浏览器严格串行**：同一时刻只有一个账号在跑无头 Chromium，内存峰值始终只有一个实例（约 500~700MB）
- **网页管理界面**：上传登录态、勾选续火花好友、设置发送时间、手动发送/干跑测试、查看日志，全部可视化操作
- **好友一键勾选**：从聊天列表自动读取好友（含各自火花天数），勾选即用，免手动输入
- **每日定时发送**：随机时间窗口 + 随机文案库 + 好友间随机间隔，模拟真人节奏
- **防错发保护**：点击后校验右侧会话标题，确认切对人再发送；搜索兜底直接打开会话
- **真实发送校验**：消息离开输入框才算发出，失败自动重试一次
- **当日自动补发**：本轮有失败时，约 45 分钟后自动只对失败好友补发一次（每天最多一次）
- **限流检测**：识别"操作频繁 / 安全验证"提示，命中立即停止本轮，避免误发
- **掉线提醒**：登录态失效时网页状态标红，重新扫码上传即可恢复
- **低配置可跑**：1 核 1G 内存的 Debian 服务器即可（部署脚本自动创建 2G swap）

## 工作原理

```text
你的电脑（一次性操作）                服务器（日常全自动）
┌───────────────────────────┐      ┌──────────────────────────────────┐
│ extract_cookie.py         │      │ FastAPI Web 服务（网页 UI）        │
│ 打开浏览器 → 手机扫码登录  │ 上传 │ APScheduler：每天定时触发         │
│ 导出登录态 state.json     │ ───▶ │ Playwright 无头浏览器             │
└───────────────────────────┘      │ 打开 douyin.com/chat              │
                                   │ 依次给勾选的好友发送随机文案      │
                                   │ 校验发送结果 / 失败自动补发       │
                                   │ 记录日志，掉线标红               │
                                   └──────────────────────────────────┘
```

登录态在**本机扫码获取、服务器只复用**，避免"机房 IP + 异地登录"触发风控；网页端消息同样计入火花。

## 目录结构

```text
app.py                    FastAPI 服务入口
extract_cookie.py         本机提取登录态脚本
core/automation.py        Playwright 自动化发送 / 好友列表读取
core/scheduler.py         每日定时与当日补发调度
core/config.py            配置读写
core/runtime.py           状态、日志与运行记录
static/index.html         网页管理界面（Vue 3 + Element Plus，本地资源）
deploy/deploy.sh          Debian/Ubuntu 一键部署脚本
deploy/douyin-spark.service  systemd 单元文件
deploy/douyin-spark@.service  多实例 systemd 模板单元
data/                     运行时生成：config.json / runtime.json / logs/
                          states/   每个账号的登录态文件：<账号ID>.json（首次升级会自动把旧的 state.json 迁移到这里）
```

## 快速开始

### 第一步：获取登录态（在你的 Windows/macOS 电脑上）

```bash
pip install -r requirements.txt
playwright install chromium
python extract_cookie.py
```

弹出的浏览器窗口里用手机抖音 App 扫码登录，脚本会在 `data/state.json` 导出登录态。

### 第二步：部署到服务器（Debian 13 / Ubuntu）

把整个项目目录上传到服务器（如 `/opt/douyin-spark`），然后以 root 执行：

```bash
sudo bash deploy/deploy.sh
```

脚本会自动完成：安装依赖、安装 Chromium、创建 2G swap（1G 内存服务器需要）、设置上海时区、生成随机访问令牌、注册 systemd 服务并启动。完成后会输出访问地址和令牌（保存在项目根目录 `.env`）。

> 1G 内存跑无头 Chromium 偏紧，脚本自动创建的 swap 是必需的。发送任务运行期间内存峰值约 500~700MB。

### 第三步：网页配置

1. 打开 `http://服务器IP:8001`，输入部署时输出的访问令牌；
2. 在「账号管理」页，把第一步生成的一个或多个登录态文件拖进上传区并上传（可一次多选）；每个文件会自动登记成一个账号；
3. 为每个账号点「配置」→ 勾选/填写续火花好友、消息模板，并可设置发送时间偏移（分钟），实现分时发送；
4. 「定时设置」页设置每天基准发送时间（默认 21:00）；各账号在此基础上叠加自己的偏移；
5. 先点「干跑测试」验证流程，再点「立即发送全部启用账号」。

之后每天自动运行。唯一的人工介入时机：网页顶部状态标红提示"登录态已过期"时，重新执行第一步，再上传该账号的登录态文件。

> 💡 **多账号分时**：在「账号管理」里给不同账号设置不同的偏移（如账号 A +0 分、账号 B +20 分、账号 C +40 分），它们会在各自时刻依次发送；浏览器任务由文件锁严格串行，不会同时跑多个 Chromium。若想全部同时开始，把所有账号的偏移都设为 0，系统仍会按队列一个接一个发送。

## 部署进阶：域名 + HTTPS

如果你的域名已解析到服务器（假设为 `douyin.example.com`）：

```bash
apt-get install -y nginx certbot python3-certbot-nginx

cat > /etc/nginx/sites-available/douyin-spark <<'EOF'
server {
    listen 80;
    server_name douyin.example.com;
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/douyin-spark /etc/nginx/sites-enabled/douyin-spark
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

certbot --nginx -d douyin.example.com --non-interactive --agree-tos \
  --register-unsafely-without-email --redirect
```

并在 `.env` 中追加 `HOST=127.0.0.1`（让后端只监听本机、由 nginx 转发），然后 `systemctl restart douyin-spark`。

## 部署进阶：单主机多实例

一个账号 = 一套独立的数据目录 + 端口 + 令牌，**代码和依赖只需一份**。核心是把数据目录通过 `DATA_DIR` 环境变量指向各实例自己的目录，其余路径（config / state / runtime / logs）都从它派生，互不干扰。

### 环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `DATA_DIR` | 数据目录（config.json / state.json / runtime.json / logs） | 代码目录下 `data/` |
| `PORT` | 监听端口 | `8001` |
| `AUTH_TOKEN` | 网页访问令牌 | 空（不校验） |
| `HOST` | 监听地址 | `0.0.0.0` |
| `ENV_FILE` | 从指定 `.env` 文件加载以上变量 | 代码目录下 `.env` |

优先级：真实环境变量 > `.env` 文件。

### 步骤

以两台实例（端口 8001 / 8002）为例，假设代码部署在 `/opt/douyin-spark`：

```bash
# 1. 准备目录
mkdir -p /var/lib/douyin-spark/1 /var/lib/douyin-spark/2 /etc/douyin-spark

# 2. 每个实例一份环境文件（放各自的令牌和端口）
cat > /etc/douyin-spark/1.env <<'EOF'
PORT=8001
AUTH_TOKEN=换成随机令牌一
EOF
cat > /etc/douyin-spark/2.env <<'EOF'
PORT=8002
AUTH_TOKEN=换成随机令牌二
EOF
chmod 600 /etc/douyin-spark/*.env

# 3. 生成模板单元（替换路径占位符）
sed "s|__DIR__|/opt/douyin-spark|g; s|__VENV__|/opt/douyin-spark/.venv|g; \
     s|__DATA_BASE__|/var/lib/douyin-spark|g; s|__ENV_DIR__|/etc/douyin-spark|g" \
    deploy/douyin-spark@.service > /etc/systemd/system/douyin-spark@.service

# 4. 启动
systemctl daemon-reload
systemctl enable --now douyin-spark@1
systemctl enable --now douyin-spark@2
```

`@` 后面的 `1`/`2` 是实例名（模板里的 `%i`），`DATA_DIR` 和 `EnvironmentFile` 会按实例名自动落到各自目录。之后每个实例**分别**：打开对应端口 → 输令牌 → 上传登录态文件 → 勾选好友 → 设发送时间。

> 🔀 **两种多账号方式**：
> 1. **单实例多账号**（推荐）：在一个实例的「账号管理」页上传多个登录态文件，每个账号独立配置、分时发送，代码只需一份、浏览器任务串行，内存占用最低；
> 2. **多实例（方案 B）**：每个账号一个独立 `DATA_DIR` + 端口 + 令牌，如上文所示，适合需要彻底隔离或分布在不同服务器的场景，但常驻内存会随实例数线性增长。

常用命令：

```bash
systemctl status douyin-spark@1
journalctl -u douyin-spark@1 -f
systemctl restart douyin-spark@2
systemctl disable --now douyin-spark@2
```

> 注意：每增加一个实例，发送时都会额外占用约 500~700MB 内存（无头 Chromium）。多个账号建议错开各自的发送时间，避免同时发送瞬间内存吃满；多账号也更容易触发抖音风控，请克制使用。

## 自动发送策略与保护机制

- **切换校验**：点击联系人后，必须确认右侧会话顶部标题出现目标昵称，才继续发送，杜绝"发给上一个人"。
- **搜索兜底**：列表点不到（懒加载/名字不在最近聊天）时，自动用搜索框查找并点「发消息」直接打开会话。
- **发送校验**：文字进入输入框 → 按 Enter → 文字离开输入框，三步都满足才算成功；失败自动重试一次。
- **当日补发**：本轮存在失败好友时，约 45 分钟后只对失败者补发一次（每个账号独立计算）；若期间一轮全部成功，补发自动取消。
- **分时发送**：每个账号可在基准发送时间上叠加自己的偏移分钟数，系统按账号分别触发定时任务，避免多个账号同时发送。
- **限流熔断**：检测到"操作频繁 / 安全验证"等提示立即停止本轮，防止连续操作触发更严厉风控。
- **节奏拟人化**：发送时间有随机抖动窗口、好友之间有随机间隔、文案从模板库随机选择。

## 常见问题

- **状态显示"登录态已过期"**：本机重新运行 `extract_cookie.py`，网页重新上传即可。登录态通常能维持几天到几周。
- **好友切换失败**：优先用「获取聊天列表」勾选（取的是聊天列表真实显示名）；手动填写时用完整备注/昵称。
- **提示操作频繁**：调大"好友间隔"、减少每次发送人数；少量好友（几个到十几个）最稳。
- **服务器 IP 被风控**：优先选与日常登录城市相同的国内机房节点；海外机房 IP 更容易触发验证码。
- **网页打不开**：确认安全组/防火墙放行 8001（或 80/443）端口，且 `systemctl status douyin-spark` 正常。

## 安全与合规

- 访问令牌为随机生成，保存在 `.env`；建议再在防火墙层把管理端口限制为只有你自己的 IP 可访问。
- `state.json` 包含账号会话信息，属敏感数据，**切勿提交到仓库或分享**（`data/` 与 `.env` 已加入 `.gitignore`）。
- 本项目违反抖音社区公约中"未经平台允许采用自动化手段发私信"的规定，账号可能被限流或封禁。仅限个人低频自用，请自行承担风险。

## 致谢

发送流程与部署思路借鉴了以下开源项目（MIT 协议或公开教程）：

- [douyin-cloud-streak](https://github.com/Yuriz132/douyin-cloud-streak)
- [DouYinSparkFlow](https://github.com/2061360308/DouYinSparkFlow)
- [TikTokAutoSparkWeb](https://github.com/DkoBot/TikTokAutoSparkWeb)

## License

[MIT](./LICENSE)
