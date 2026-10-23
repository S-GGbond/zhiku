#!/usr/bin/env bash
# 在 Linux 服务器上安装 zhiku 为 systemd 服务（开机自启、崩溃自拉起）。
#
#   sudo ./deploy/install.sh                 # 装到 /opt/zhiku，用户 zhiku
#   sudo ./deploy/install.sh --dir /srv/zhiku --port 8000
#   sudo ./deploy/install.sh --in-place      # 直接就地用当前仓库目录，不复制
#
# 重复执行是安全的：会更新代码、依赖，然后重启服务。

set -euo pipefail

NAME="zhiku"
APP_DIR="/opt/zhiku"
SERVICE_USER="zhiku"
HOST="127.0.0.1"
PORT="8000"
COPY_MODE=1

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) APP_DIR="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --in-place) COPY_MODE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1（--help 查看用法）" >&2; exit 1 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "需要 root 权限，请用 sudo 执行"
command -v systemctl >/dev/null || die "没有 systemd，这个脚本只支持 systemd 的发行版"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SRC_DIR/cli.py" ]] || die "找不到 $SRC_DIR/cli.py，请在仓库目录里执行 deploy/install.sh"

if [[ $COPY_MODE -eq 0 || "$SRC_DIR" == "$APP_DIR" ]]; then
  APP_DIR="$SRC_DIR"
  log "就地安装：$APP_DIR"
else
  log "安装到：$APP_DIR（源：$SRC_DIR）"
  mkdir -p "$APP_DIR"
  tar -C "$SRC_DIR" \
      --exclude=./data --exclude=./.git --exclude=__pycache__ \
      --exclude=./.venv --exclude=./.env --exclude='*.pyc' \
      -cf - . | tar -C "$APP_DIR" -xf -
fi

# ---------- 账号 ----------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  log "创建系统用户：$SERVICE_USER"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
    || warn "创建用户失败，将用 root 运行（不建议）"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER" 2>/dev/null || echo root)"

# ---------- 数据与配置 ----------
mkdir -p "$APP_DIR/data"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  log "已生成 $APP_DIR/.env（稍后可在 WebUI 里填模型接口）"
fi

# ---------- Python 环境 ----------
command -v python3 >/dev/null || die "缺少 python3，请先安装：apt install -y python3 python3-venv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  log "创建虚拟环境"
  python3 -m venv "$APP_DIR/.venv" || die "创建 venv 失败，可能需要安装 python3-venv"
fi
log "安装依赖（首次会慢一些）"
"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"

# ---------- systemd ----------
UNIT="/etc/systemd/system/${NAME}.service"
TEMPLATE="$APP_DIR/deploy/${NAME}.service"
[[ -f "$TEMPLATE" ]] || die "缺少 $TEMPLATE"

log "写入 $UNIT"
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
    -e "s|@HOST@|$HOST|g" \
    -e "s|@PORT@|$PORT|g" \
    "$TEMPLATE" > "$UNIT"
chmod 644 "$UNIT"

# 端口被别的进程占着时给个提醒（重装场景下旧实例还活着，不算冲突）
if ! systemctl is-active --quiet "$NAME"; then
  if "$APP_DIR/.venv/bin/python" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT)) == 0 else 1)"; then
    warn "端口 $PORT 已被其他进程占用，服务可能起不来（lsof -i :$PORT 看看是谁）"
  fi
fi

systemctl daemon-reload
systemctl enable "$NAME" >/dev/null 2>&1 || true
systemctl restart "$NAME"

# ---------- 健康检查 ----------
log "等待服务就绪"
ok=0
for _ in $(seq 1 30); do
  if "$APP_DIR/.venv/bin/python" -c "import sys,urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/health', timeout=2).read()" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done

echo
if [[ $ok -eq 1 ]]; then
  printf '\033[1;32m✓ 已启动\033[0m  监听 %s:%s（仅本机）\n' "$HOST" "$PORT"
else
  warn "30 秒内没等到 /health，看日志：journalctl -u $NAME -n 50 --no-pager"
fi

cat <<EOF

服务名：$NAME
状态：systemctl status $NAME
日志：journalctl -u $NAME -f
配置：$APP_DIR/.env  或 WebUI「设置」
数据：$APP_DIR/data（一个 kb.sqlite3，备份直接拷它）

下一步：
1) 对外开放需自备反向代理并配 TLS，例如 Caddy：
     zhiku.example.com { reverse_proxy 127.0.0.1:$PORT }
   不建议直接把 --host 改成 0.0.0.0 裸奔公网。
2) 浏览器打开后先创建账号，再到「设置」填向量与对话模型接口。
3) 升级：git pull && sudo ./deploy/install.sh
EOF
