#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
    echo "未检测到虚拟环境，正在创建..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

source .venv/bin/activate
exec python UnipusAI_Helper.py "$@"
