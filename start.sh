#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env，请按需填写向量模型接口后重试"
fi

python3 -c "import fastapi, uvicorn, openai, numpy, jieba" 2>/dev/null || {
  echo "缺少依赖，正在安装…"
  python3 -m pip install -r requirements.txt
}

exec python3 cli.py serve "$@"
