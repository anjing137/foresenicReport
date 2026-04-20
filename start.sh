#!/bin/bash
# 司法鉴定意见书系统 - 一键启动

cd "$(dirname "$0")"
BASEDIR=$(pwd)

echo "🚀 启动司法鉴定意见书系统..."

# 检查是否已在运行
if lsof -i :8000 -t &>/dev/null; then
    echo "⚠️  后端已在运行 (端口 8000)"
else
    echo "  启动后端..."
    cd "$BASEDIR/backend"
    nohup ~/.venv_vlm/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/forensic_backend.log 2>&1 &
    echo "  后端启动中..."
fi

if lsof -i :3000 -t &>/dev/null; then
    echo "⚠️  前端已在运行 (端口 3000)"
else
    echo "  启动前端..."
    cd "$BASEDIR/frontend"
    nohup npm run dev > /tmp/forensic_frontend.log 2>&1 &
    echo "  前端启动中..."
fi

# 等待服务就绪
sleep 3

echo ""
echo "✅ 启动完成！"
echo "   前端：http://localhost:3000"
echo "   后端：http://localhost:8000"
echo "   文档：http://localhost:8000/docs"
echo ""
echo "   日志：tail -f /tmp/forensic_backend.log"
echo "         tail -f /tmp/forensic_frontend.log"
