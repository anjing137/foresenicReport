#!/bin/bash
# 启动司法鉴定系统后端
# 使用已有的 PaddleOCR 环境

cd "$(dirname "$0")"

# 激活已有的 PaddleOCR 环境
source /Users/anjing137/.venv_vlm/bin/activate

# 创建必要的目录
mkdir -p uploads reports templates models

# 启动服务
echo "🚀 启动后端服务..."
python main.py
