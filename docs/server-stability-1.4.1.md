# 1.4.1 部署与诊断

## 兼容性及备份

不更换签名密钥，不修改数据结构，不改客户端API的原字段和错误提示。
备份原镜像、Compose配置、Nginx配置及签名私钥。SQLite运行时备份请使用
SQLite backup API，不直接复制正在写入的数据库文件（WAL可能尚未合并）。
回滚应用时恢复旧镜像/配置，不默认回滚数据库，以免丢失升级后的业务数据。

设置 `HEARTBEAT_INTERVAL=60`、`ONLINE_TIMEOUT=180`、`DEVICE_BINDING_TTL=900`，
并保留原数据挂载、后台凭证和签名私钥。新参数需要重新创建容器，仅重启
容器不会读入修改后的Compose环境变量。

## 反向代理

将 `nginx-license-observability.conf` 装到Nginx http级配置目录。在本服务
各server块中添加：

```nginx
access_log /var/log/nginx/license-timing.log license_timing;
```

在代理location中覆盖来自外部的诊断/客户端IP头：

```nginx
proxy_set_header X-Request-ID $request_id;
proxy_set_header X-Client-IP $remote_addr;
proxy_hide_header X-Request-ID;
add_header X-Request-ID $request_id always;
```

注意：若location已有其他`add_header`或依赖server级HSTS，不要盲目新增
location级`add_header`。当前部署在server级设置响应请求ID和HSTS，
location仅传递请求ID并隐藏后端同名响应头。

走Cloudflare时，应先按Cloudflare官方IP段配置`set_real_ip_from`，
配合`real_ip_header CF-Connecting-IP`和`real_ip_recursive on`，再使用
`$remote_addr`。不要直接信任公网客户端自己提供的`CF-Connecting-IP`。
官方列表来自 `https://www.cloudflare.com/ips-v4` 与
`https://www.cloudflare.com/ips-v6`；部署时重新读取验证，不永久假设IP段不变。
API本身继续仅绑定本机端口，由受信代理覆盖客户端IP头。

`nginx -t`通过后再reload。为`license-timing.log`配置日志轮转（Debian
默认`/var/log/nginx/*.log`规则已覆盖它）。Docker日志最大10MB，保留5份。

## 如何定位超时

- Nginx日志有请求但没有对应应用请求ID：排查代理到应用的连接和进程状态。
- 应用`duration_ms`很长：排查数据库锁等待及后端处理。
- Nginx返回429或应用`status=429`：检查该出口总预算及单设备请求频率。
- 客户端超时且两侧都找不到该次请求：进一步排查客户端、VPN、DNS及到
  Cloudflare/源站的链路；仅凭一次缺失日志不能确定具体故障点。

Nginx计时单位为秒，应用计时为毫秒。成功的授权拒绝可能仍是HTTP200，
具体业务错误仍从现有心跳日志页查看，不能只凭HTTP200判断卡密有效。
日志不包含完整卡密、请求体、查询参数和管理Cookie。
