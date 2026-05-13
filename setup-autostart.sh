#!/bin/bash
# Session Memory 自动启动安装脚本
# 用法: sudo bash setup-autostart.sh

set -euo pipefail

SERVICE_NAME="session-memory"
SERVICE_FILE="session-memory.service"
PROJECT_DIR="/home/jimwong/session-memory"

echo "=== Session Memory 自动启动配置 ==="

# 检查 docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装，请先安装 Docker"
    exit 1
fi

# 检查 docker compose
if ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose 未安装，请先安装 Docker Compose"
    exit 1
fi

# 创建 .env（如不存在）
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "📝 从 .env.example 创建 .env ..."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "⚠️  请编辑 $PROJECT_DIR/.env 填入 OPENAI_API_KEY"
fi

# 安装 systemd service
echo "📦 安装 systemd 服务..."
cp "$PROJECT_DIR/$SERVICE_FILE" /etc/systemd/system/
systemctl daemon-reload

# 启用开机自启
echo "🔧 启用开机自启..."
systemctl enable "$SERVICE_NAME"

# 立即启动
echo "🚀 启动服务..."
systemctl start "$SERVICE_NAME"

# 检查状态
echo ""
echo "=== 服务状态 ==="
systemctl status "$SERVICE_NAME" --no-pager || true

echo ""
echo "✅ 配置完成！"
echo ""
echo "常用命令:"
echo "  查看状态:  sudo systemctl status $SERVICE_NAME"
echo "  查看日志:  sudo docker compose -f $PROJECT_DIR/docker-compose.yml logs -f"
echo "  重启服务:  sudo systemctl restart $SERVICE_NAME"
echo "  停止服务:  sudo systemctl stop $SERVICE_NAME"
echo "  禁用自启:  sudo systemctl disable $SERVICE_NAME"
echo ""
echo "API 地址:  http://localhost:8000"
echo "API 文档:  http://localhost:8000/docs"
