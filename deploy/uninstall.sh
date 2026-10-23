#!/usr/bin/env bash
# 卸载 zhiku 服务。默认保留代码与数据，加 --purge 会一并删除目录。

set -euo pipefail

NAME="zhiku"
APP_DIR="/opt/zhiku"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) APP_DIR="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) echo "用法：sudo ./deploy/uninstall.sh [--dir /opt/zhiku] [--purge]"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done

[[ $(id -u) -eq 0 ]] || { echo "需要 root 权限" >&2; exit 1; }

systemctl disable --now "$NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${NAME}.service"
systemctl daemon-reload
echo "服务已移除"

if [[ $PURGE -eq 1 ]]; then
  read -r -p "删除 $APP_DIR 及其中所有数据？输入 yes 确认：" ans
  [[ "$ans" == "yes" ]] && rm -rf "$APP_DIR" && echo "已删除 $APP_DIR" || echo "已保留 $APP_DIR"
else
  echo "代码与数据保留在 $APP_DIR"
fi
