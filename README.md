# License Server

轻量级卡密验证服务端，基于 FastAPI + SQLite，支持多项目管理、设备绑定、心跳检测。

## 功能

- **多项目管理** — 一套服务管理多个软件产品的卡密
- **卡密生成** — 批量生成，支持设置有效期、最大设备数
- **设备绑定** — 基于机器码自动绑定，限制并发设备数
- **心跳检测** — 客户端定时上报，超时自动释放设备名额
- **吊销/启用** — 随时控制卡密状态
- **Web 管理面板** — 暗色主题，Lucide 图标，响应式布局

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```env
ADMIN_KEY=your-admin-secret-key    # 管理面板登录密钥
DATABASE_URL=sqlite+aiosqlite:///./data/license.db
HEARTBEAT_TIMEOUT=120              # 心跳超时（秒）
HOST=0.0.0.0
PORT=9000
```

### 3. 启动

```bash
python main.py
```

访问 `http://localhost:9000` 进入管理面板。

## API 接口

### 客户端接口（无需鉴权）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/verify` | 验证卡密 + 设备绑定 |
| POST | `/api/heartbeat` | 心跳保活 |

### 管理接口（需要 `X-Admin-Key` 请求头）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/projects` | 查询所有项目 |
| POST | `/admin/projects` | 创建项目 |
| PUT | `/admin/projects/{code}` | 更新项目 |
| DELETE | `/admin/projects/{code}` | 删除项目 |
| GET | `/admin/licenses?project=xxx` | 查询卡密（可按项目筛选） |
| POST | `/admin/licenses` | 批量生成卡密 |
| POST | `/admin/revoke` | 吊销卡密 |
| POST | `/admin/activate` | 启用卡密 |
| POST | `/admin/unbind` | 解绑设备 |
| GET | `/admin/devices/{license_key}` | 查看绑定设备 |

### 客户端验证示例

```python
import aiohttp

async def verify():
    async with aiohttp.ClientSession() as session:
        async with session.post("http://your-server:9000/api/verify", json={
            "license_key": "your-license-key",
            "device_id": "machine-hash",
            "device_info": "Windows 11 / x86_64",
            "project": "vidu2api"
        }) as resp:
            data = await resp.json()
            if data["ok"]:
                print(f"验证通过，心跳间隔: {data['heartbeat_interval']}s")
            else:
                print(f"验证失败: {data['error']}")
```

## 技术栈

- **后端**: FastAPI + SQLAlchemy + aiosqlite
- **数据库**: SQLite（可替换为 PostgreSQL/MySQL）
- **前端**: 原生 HTML/CSS/JS + Lucide Icons

## 项目结构

```
license-server/
├── main.py              # 应用入口
├── api.py               # API 路由
├── db.py                # 数据库模型
├── requirements.txt     # 依赖
├── .env.example         # 环境变量模板
├── static/
│   ├── index.html       # 管理面板页面
│   └── app.js           # 前端逻辑
└── data/
    └── license.db       # SQLite 数据库（自动创建）
```

## License

MIT
