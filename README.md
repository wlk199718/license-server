# License Server

轻量级卡密验证服务端，基于 FastAPI + SQLAlchemy，支持多项目、设备绑定、心跳检测、RSA 签名响应和 Web 管理面板。

## 主要能力

- 多项目管理和项目停用
- 批量生成卡密、有效期、首次激活计时和设备上限
- 设备绑定、超时释放和设备解绑
- RSA-SHA256 签名验证响应，客户端可校验响应来源
- HttpOnly 管理会话 Cookie + CSRF 防护
- 参数校验、请求限流、健康检查、审计日志和安全响应头
- 后台按编号/备注、项目和状态筛选；生成卡密防重复提交与请求去重
- 后台审计页、待激活状态、独立吊销统计和项目删除二次确认
- 图标资源自托管（Lucide，见 `static/vendor/LUCIDE-LICENSE.txt`）
- SQLite WAL；生产多实例建议使用 PostgreSQL

## 快速开始

```bash
pip install -r requirements.txt
Copy-Item .env.example .env
```

在 `.env` 中设置两个不同、至少 32 字符的随机密钥：

```env
ADMIN_KEY=使用随机生成的管理员密钥
SESSION_SECRET=使用另一个随机生成的会话密钥
DATABASE_URL=sqlite+aiosqlite:///./data/license.db
DATA_DIR=./data
HEARTBEAT_INTERVAL=60
ONLINE_TIMEOUT=180
DEVICE_BINDING_TTL=900
SESSION_TTL=2592000
COOKIE_SECURE=false
SIGNING_PRIVATE_KEY_PATH=./data/license_private.pem
HOST=0.0.0.0
PORT=9000
```

启动：

```bash
python main.py
```

访问 `http://localhost:9000`。生产环境应通过 HTTPS 反向代理访问，并把 `COOKIE_SECURE` 设置为 `true`。

如果签名私钥路径不存在，服务会在持久化数据目录自动生成私钥。也可以通过 `SIGNING_PRIVATE_KEY_PATH` 注入由运维管理的私钥。客户端公钥可从 `/api/public-key` 获取并固化到客户端，不能在每次启动时无条件信任远程下载的公钥。

**升级已有部署前**：备份 `data/license.db` 和原签名私钥，并将原来的 `keys/license_private.pem` 复制到持久化目录 `data/license_private.pem`。否则新版本自动生成新密钥后，旧客户端内置的公钥将无法验证响应。

## Docker 部署

```bash
cp .env.example .env
# 编辑 .env 设置 ADMIN_KEY、SESSION_SECRET
mkdir -p data
docker compose up -d
```

容器默认只在宿主机 `127.0.0.1:9000` 监听，必须通过 HTTPS 反向代理开放服务。`data` 目录必须持久化，其中包含数据库和自动生成的签名私钥。不要把 `.env`、数据库或私钥提交到 Git。

## 稳定性设置（1.4.1）

| 设置 | 默认 | 含义 |
|---|---:|---|
| `HEARTBEAT_INTERVAL` | 60 秒 | 通过原 `heartbeat_interval` 字段告知客户端 |
| `ONLINE_TIMEOUT` | 180 秒 | 超时在后台显示离线，不立即解绑 |
| `DEVICE_BINDING_TTL` | 900 秒 | 无心跳达到此时间释放绑定及设备名额 |
| `VERIFY_IP_LIMIT` | 1200 次/分钟 | 共享公司/VPN出口的验证总预算 |
| `HEARTBEAT_IP_LIMIT` | 6000 次/分钟 | 共享出口的心跳总预算 |
| `VERIFY_DEVICE_LIMIT` | 12 次/分钟 | 有效卡密+设备的验证预算，与IP无关 |
| `HEARTBEAT_DEVICE_LIMIT` | 12 次/分钟 | 有效卡密+设备的心跳预算，与IP无关 |
| `INVALID_LICENSE_IP_LIMIT` | 120 次/分钟 | 同IP无效卡密尝试，验证和心跳共用 |

在线时间必须至少为心跳间隔的两倍，绑定保留时间不能小于在线时间。旧 `HEARTBEAT_TIMEOUT` 仅在未设置 `ONLINE_TIMEOUT` 时作为在线时间兼容值，不再控制心跳间隔或解绑时间。Docker配置显式使用新变量。

离线设备在保留期内仍占名额；急需换机请在后台解绑。保留期不延长卡密有效期，吊销、到期、项目停用及设备上限校验保持不变。超过保留期的心跳仍返回原“设备未注册，请重新启动程序”，新客户端可正常重新验证，旧客户端协议没有改变。

限流保留 `429` 和 `Retry-After`，管理员登录仍为每IP每5分钟5次。限流桶在单进程内存中，重启会清空；多worker/多实例部署须先换成共享限流存储，不能直接把当前每进程额度当全局额度。

设备连续 10 次出现卡密不存在、密钥错误、吊销、到期或项目不匹配，会自动进入持久化黑名单；验证和心跳合并计数，成功验证或心跳清零。设备未注册、设备名额已满、项目停用、网络错误及限流不计入卡密错误。拉黑后验证和心跳直接返回带签名的 HTTP 403，不再新增心跳日志；后台和其他共享 IP 的设备不受影响。管理员可在心跳页面的设备黑名单中解除。服务重启和日志清理不会解除黑名单。设备标识由客户端上报，因此这属于服务端拒绝策略，不能阻止被修改的客户端换设备标识或继续发送网络请求。

服务输出JSON请求诊断日志：请求ID、路由模板、HTTP状态、耗时、客户端IP；不记录正文、查询参数、卡密、Cookie或原始异常消息。响应增加 `X-Request-ID`、`X-Response-Time-Ms`，不改签名JSON。使用自定义Uvicorn命令时加 `--no-access-log`，避免默认访问日志再次记录原始URL。Docker日志按10MB×5轮转；心跳数据库日志默认保留最近24小时（`HEARTBEAT_LOG_RETENTION_DAYS=1`），每分钟清理一次，更早的记录自动删除，操作审计日志保持原策略。

Nginx关联日志模板见 `docs/nginx-license-observability.conf`，配置步骤见 `docs/server-stability-1.4.1.md`。升级必须保留原 `data` 挂载和签名私钥；不需重发卡密或再更新客户端。

## API

### 客户端接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/verify` | 验证卡密并绑定设备 |
| POST | `/api/heartbeat` | 设备心跳 |
| GET | `/api/public-key` | 获取服务端签名公钥 |
| GET | `/healthz` | 健康检查 |

`/api/verify` 和 `/api/heartbeat` 的成功或失败响应均带有 `signature`、`signature_alg`、`key_id`、`signed_at` 和 `response_expires_at`。客户端每次请求使用随机 `request_id`，并用内置的固定公钥验证签名、响应时效、设备与项目标识；可参考 [`client_verifier.py`](client_verifier.py)。轮换签名密钥时，应先发布内置新公钥的客户端，再切换服务端私钥。

新卡密格式为 `编号.随机密钥`。1.4.2起保留SHA-256摘要用于客户端校验，并加密保存完整卡密供已登录管理员在列表中反复查看、复制；刷新或重新登录不会丢失。展示密钥默认位于持久化目录 `data/license_display.key`，务必与数据库一起备份，不能删除或随意更换。管理接口禁止缓存。旧版明文卡密可直接显示；历史上仅保存摘要的卡密无法还原，列表显示“仅编号”，其授权保持有效。`starts_on_first_activation=true` 可让有效期从首次成功激活时开始计算。

管理端生成卡密时可传 `request_id`（16–64 位字母数字、`_`、`-`），同一请求编号只会创建一次。若响应丢失，重试同一编号返回409，请到卡密列表查询并复制，不重复生成。管理端删除项目时必须传 `expected_count` 查询参数，服务端会核对卡密数；浏览器还会要求输入项目标识。直接调用管理 API 时也需要先查询数量并确认不可恢复的级联删除。

### 管理接口

浏览器先调用 `POST /admin/login` 登录。服务端通过 HttpOnly Cookie 保存会话，写操作还需要 `X-CSRF-Token`。前端已内置该流程，不再把管理员密钥保存到 `localStorage`。

后台登录有效期默认 30 天（`SESSION_TTL=2592000` 秒），从登录成功开始计时，管理员密码本身不会过期。修改该配置后需重新创建容器并重新登录，已签发的会话仍使用原到期时间。

如需使用自定义后台密码，可以设置 `ADMIN_PASSWORD_HASH`（带随机盐的 PBKDF2-SHA256，至少 600000 次迭代）。配置后后台登录仅接受该密码；`ADMIN_KEY` 仍须保持独立随机值，用于旧版 `X-Admin-Key` 调用。切勿把明文密码写入配置。通过公网 IP 的 HTTP 登录会明文传输密码，应尽快改用 HTTPS 域名，并设置 `COOKIE_SECURE=true`。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/login` | 管理员登录 |
| POST | `/admin/logout` | 退出登录 |
| GET | `/admin/me` | 检查会话 |
| GET | `/admin/stats` | 聚合统计 |
| GET | `/admin/audit` | 分页审计日志 |
| GET/POST/PUT/DELETE | `/admin/projects` | 项目管理 |
| GET/POST | `/admin/licenses` | 查询和生成卡密 |
| POST | `/admin/revoke` | 吊销卡密并清理设备 |
| POST | `/admin/activate` | 启用卡密 |
| POST | `/admin/update` | 修改卡密 |
| POST | `/admin/delete` | 删除卡密 |
| POST | `/admin/unbind` | 解绑设备 |
| GET | `/admin/devices/{license_key}` | 查询设备 |

旧版客户端仍可暂时使用 `X-Admin-Key` 访问管理接口，但新前端不再使用该方式，建议尽快迁移。

## 测试

```bash
python -m pytest -q
node --test tests/test_frontend.cjs
node --check static/app.js
python -m compileall -q .
```

## 项目结构

```
license-server/
├── main.py
├── api.py
├── db.py
├── client_verifier.py
├── requirements.txt
├── static/
├── tests/
└── data/                 # 运行时数据，不提交
```

## License

MIT
