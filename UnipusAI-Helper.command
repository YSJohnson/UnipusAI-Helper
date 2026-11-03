#!/bin/bash
set -e

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ ! -d ".venv" ]; then
    echo "未检测到虚拟环境，正在初始化..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

source .venv/bin/activate
python UnipusAI_Helper.py "$@"

EXIT_STATUS=$?
if [ $EXIT_STATUS -ne 0 ]; then
    echo ""
    echo "程序已退出，状态码: $EXIT_STATUS"
    read -n 1 -s -r -p "按任意键关闭窗口..."
    echo ""
fi
