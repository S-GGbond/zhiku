#!/usr/bin/env bash
# 服务器上更新到最新代码：拉取后重新安装（保留 data/ 与 .env），最后重启服务。
#
#   sudo ./deploy/update.sh                  # 默认 /opt/zhiku
#   sudo ./deploy/update.sh /srv/zhiku       # 自定义安装目录
#
# 两种部署方式都能处理：
#   - 默认安装（install.sh 复制代码到 /opt/zhiku）：回到源码目录 git pull，再重新复制
#   - --in-place 安装（服务直接用仓库目录）：直接在安装目录 git pull

set -euo pipefail

APP_DIR="${1:-/opt/zhiku}"
shift || true
NAME="zhiku"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "需要 root 权限：sudo ./deploy/update.sh"
command -v git >/dev/null || die "缺少 git"

SRC=""
if [[ -f "$APP_DIR/.install-source" ]]; then
  SRC="$(cat "$APP_DIR/.install-source")"
fi

if [[ -n "$SRC" && -d "$SRC/.git" ]]; then
  log "源码目录：$SRC"
  git -C "$SRC" pull --ff-only
  log "版本：$(git -C "$SRC" log --oneline -1)"
  exec "$SRC/deploy/install.sh" --dir "$APP_DIR" "$@"
elif [[ -d "$APP_DIR/.git" ]]; then
  log "就地更新：$APP_DIR"
  git -C "$APP_DIR" pull --ff-only
  log "版本：$(git -C "$APP_DIR" log --oneline -1)"
  exec "$APP_DIR/deploy/install.sh" --in-place "$@"
else
  die "找不到 git 仓库。请在服务器上的克隆目录执行：git pull && sudo ./deploy/install.sh --dir $APP_DIR"
fi
