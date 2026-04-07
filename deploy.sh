#!/bin/bash
#
# License Server 一键部署脚本
# 用法: bash <(curl -sL https://raw.githubusercontent.com/wlk199718/license-server/main/deploy.sh)
#

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

INSTALL_DIR="/opt/license-server"
COMPOSE_URL="https://raw.githubusercontent.com/wlk199718/license-server/main/docker-compose.yml"

echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}   License Server 一键部署脚本${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}[错误] 未检测到 Docker，请先安装 Docker${NC}"
    echo -e "${YELLOW}提示: 可使用 kejilion.sh 脚本安装 Docker${NC}"
    exit 1
fi

# 检查 docker compose
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo -e "${RED}[错误] 未检测到 Docker Compose，请先安装${NC}"
    exit 1
fi

# 如果已安装，提供选项
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}检测到已有安装，请选择操作:${NC}"
    echo "  1) 更新 (拉取最新镜像并重启)"
    echo "  2) 卸载 (停止并删除容器和数据)"
    echo "  3) 重新安装"
    echo "  4) 查看状态"
    echo "  0) 退出"
    echo ""
    read -p "请输入选项 [0-4]: " choice

    case $choice in
        1)
            echo -e "${GREEN}[更新] 拉取最新镜像...${NC}"
            cd "$INSTALL_DIR"
            $COMPOSE_CMD pull
            $COMPOSE_CMD up -d
            echo -e "${GREEN}[完成] 更新成功!${NC}"
            exit 0
            ;;
        2)
            read -p "确定要卸载吗？数据将被删除 [y/N]: " confirm
            if [[ "$confirm" =~ ^[Yy]$ ]]; then
                echo -e "${RED}[卸载] 停止并删除容器...${NC}"
                cd "$INSTALL_DIR"
                $COMPOSE_CMD down -v
                cd /
                rm -rf "$INSTALL_DIR"
                echo -e "${GREEN}[完成] 已卸载${NC}"
            fi
            exit 0
            ;;
        3)
            echo -e "${YELLOW}[重装] 停止旧容器...${NC}"
            cd "$INSTALL_DIR"
            $COMPOSE_CMD down
            cd /
            rm -rf "$INSTALL_DIR"
            ;;
        4)
            cd "$INSTALL_DIR"
            $COMPOSE_CMD ps
            echo ""
            $COMPOSE_CMD logs --tail=20
            exit 0
            ;;
        0)
            exit 0
            ;;
        *)
            echo -e "${RED}无效选项${NC}"
            exit 1
            ;;
    esac
fi

# 全新安装
echo -e "${GREEN}[1/4] 创建安装目录...${NC}"
mkdir -p "$INSTALL_DIR/data"
cd "$INSTALL_DIR"

echo -e "${GREEN}[2/4] 下载配置文件...${NC}"
curl -sL "$COMPOSE_URL" -o docker-compose.yml

echo -e "${GREEN}[3/4] 配置管理员密钥...${NC}"
echo ""
read -p "请设置管理员密钥 (直接回车使用随机密钥): " admin_key

if [ -z "$admin_key" ]; then
    admin_key=$(openssl rand -hex 16)
    echo -e "${YELLOW}已生成随机密钥: ${CYAN}${admin_key}${NC}"
fi

# 写入 .env
cat > .env << EOF
ADMIN_KEY=${admin_key}
HEARTBEAT_TIMEOUT=120
EOF

echo ""
echo -e "${GREEN}[4/4] 启动服务...${NC}"
$COMPOSE_CMD pull
$COMPOSE_CMD up -d

# 等待启动
echo -n "等待服务启动"
for i in {1..10}; do
    sleep 1
    echo -n "."
    if curl -s http://localhost:9000 > /dev/null 2>&1; then
        break
    fi
done
echo ""

# 获取服务器 IP
SERVER_IP=$(curl -s4 ifconfig.me 2>/dev/null || curl -s4 ip.sb 2>/dev/null || echo "your-server-ip")

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${GREEN}   部署完成!${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""
echo -e "  管理面板:  ${CYAN}http://${SERVER_IP}:9000${NC}"
echo -e "  管理密钥:  ${CYAN}${admin_key}${NC}"
echo -e "  安装目录:  ${INSTALL_DIR}"
echo -e "  数据目录:  ${INSTALL_DIR}/data"
echo ""
echo -e "${YELLOW}常用命令:${NC}"
echo -e "  查看日志:  cd ${INSTALL_DIR} && ${COMPOSE_CMD} logs -f"
echo -e "  重启服务:  cd ${INSTALL_DIR} && ${COMPOSE_CMD} restart"
echo -e "  停止服务:  cd ${INSTALL_DIR} && ${COMPOSE_CMD} down"
echo -e "  更新服务:  重新运行此脚本选择 '更新'"
echo ""
echo -e "${YELLOW}请妥善保管管理员密钥!${NC}"
echo -e "${CYAN}============================================${NC}"
