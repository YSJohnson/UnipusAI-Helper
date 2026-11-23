#!/bin/bash

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR" || exit 1

pause_and_exit() {
    echo ""
    echo "$1"
    read -n 1 -s -r -p "按任意键关闭窗口..."
    echo ""
    exit 1
}

if [ ! -d ".venv" ]; then
    echo "未检测到虚拟环境，正在初始化..."
    python3 -m venv .venv || pause_and_exit "创建虚拟环境失败，请确认已安装 python3。"
    .venv/bin/pip install -r requirements.txt || pause_and_exit "依赖安装失败，请检查网络后重试。"
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
