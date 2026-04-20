#!/bin/bash
# 司法鉴定意见书系统 - 一键关闭

echo "🛑 关闭司法鉴定意见书系统..."

# 关闭后端
if lsof -i :8000 -t &>/dev/null; then
    kill $(lsof -i :8000 -t) 2>/dev/null
    echo "  ✅ 后端已关闭 (端口 8000)"
else
    echo "  ⏭️  后端未在运行"
fi

# 关闭前端
if lsof -i :3000 -t &>/dev/null; then
    kill $(lsof -i :3000 -t) 2>/dev/null
    echo "  ✅ 前端已关闭 (端口 3000)"
else
    echo "  ⏭️  前端未在运行"
fi

echo ""
echo "已全部关闭。"
