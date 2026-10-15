#!/usr/bin/env bash
# 批量部署 N 个抖音续火花实例（多账号，方案 B：单份代码 + 每账号独立数据目录）。
#
# 严格「不同时发」：靠 core/lock.py 的跨进程文件锁排队，任意时刻只有一个实例在跑
# Chromium（发送 / 获取好友列表），其余实例阻塞等待。因此所有实例用同一个发送时刻
# 即可，不需要错峰时间表。
#
# 用法：sudo bash deploy/deploy_multi.sh <实例数N> [起始端口] [发送小时]
# 例：  sudo bash deploy/deploy_multi.sh 30 8001 20
#       -> 30 个实例，端口 8001~8030，每天 20:00 一起触发、排队串行发送。
#
# 前提：已先完成单实例部署（代码 + .venv + Chromium 就位，见 deploy/deploy.sh）。
#
# ⚠️ 内存务必先评估：30 个实例常驻约 2.2~3GB（每个 FastAPI 进程约 70~100MB），
#   加上串行时 1 个 Chromium 约 500~700MB。锁把发送峰值压到最低，但常驻内存仍是
#   硬约束——至少需要 4G 以上内存的机器。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请以 root 运行：sudo bash deploy/deploy_multi.sh <实例数N> [起始端口] [发送小时]"
  exit 1
fi

N="${1:-1}"
START_PORT="${2:-8001}"
START_HOUR="${3:-20}"        # 发送时刻（24 小时制，默认晚上 8 点）

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$SERVICE_DIR/.venv"
DATA_BASE="/var/lib/douyin-spark"
ENV_DIR="/etc/douyin-spark"
UNIT_SRC="$SERVICE_DIR/deploy/douyin-spark@.service"
UNIT_DST="/etc/systemd/system/douyin-spark@.service"

[ -x "$VENV/bin/python" ] || { echo "未找到 $VENV，请先完成单实例部署（deploy/deploy.sh）"; exit 1; }
[ -f "$UNIT_SRC" ] || { echo "缺少 $UNIT_SRC"; exit 1; }

mkdir -p "$DATA_BASE" "$ENV_DIR"

# 安装 @ 模板单元（替换路径占位符）
sed "s|__DIR__|$SERVICE_DIR|g; s|__VENV__|$VENV|g; \
     s|__DATA_BASE__|$DATA_BASE|g; s|__ENV_DIR__|$ENV_DIR|g" \
  "$UNIT_SRC" > "$UNIT_DST"
systemctl daemon-reload

echo "==> 部署 $N 个实例（端口 $START_PORT~$((START_PORT + N - 1))，每天 $START_HOUR:00 排队串行发送）"

for i in $(seq 1 "$N"); do
  PORT=$((START_PORT + i - 1))
  TOKEN="$(head -c 24 /dev/urandom | sha256sum | head -c 32)"

  # 每个实例一份环境文件（端口 + 令牌）
  cat > "$ENV_DIR/$i.env" <<EOF
PORT=$PORT
AUTH_TOKEN=$TOKEN
EOF
  chmod 600 "$ENV_DIR/$i.env"

  # 数据目录
  mkdir -p "$DATA_BASE/$i"

  # 所有实例同一时刻触发，靠跨进程锁排队严格串行发送，实现「不同时发」。
  SCHED=$(printf '%02d:00' "$START_HOUR")
  cat > "$DATA_BASE/$i/config.json" <<EOF
{
  "schedule_time": "$SCHED",
  "jitter_minutes": 2,
  "send_gap_min": 6,
  "send_gap_max": 12,
  "max_friends_per_run": 20,
  "friends": [],
  "messages": ["🔥 续火花"]
}
EOF

  systemctl enable --now "douyin-spark@$i" >/dev/null 2>&1
  echo "实例 $i：端口 $PORT，发送 $SCHED，令牌 $TOKEN"
done

echo ""
echo "完成。访问 http://<服务器IP>:<端口>，令牌见 $ENV_DIR/<编号>.env"
echo "管理：systemctl status douyin-spark@1 / journalctl -u douyin-spark@1 -f"
echo "提示：30 个端口暴露麻烦时，可用 nginx 按子域名/路径反代到各本地端口。"
