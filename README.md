# 京东云无线宝 · 客户端上下线监控（Docker）

> 路由器在**外地**也能用：不扫局域网，走**京东云无线宝 APP 同一套云端 API**。  
> 轮询接入设备 → SQLite 记上线/离线时间 → **网页表格实时刷新** → 每个设备可**自定义名字**。

本地路径：`/root/jdc-client-monitor`  
网页默认：`http://<宿主机IP>:8780`

---

## 你能得到什么

| 能力 | 说明 |
|------|------|
| 远程监控 | 路由器不在身边，只要 APP 账号 `wskey` 有效 |
| 设备表 | MAC / IP / 主机名 / 所属路由 / 在线状态 |
| 上下线时间 | `first_seen` / `last_online` / `last_offline` + 事件流水 |
| 自定义名字 | 网页点「改名」，写入 `data/aliases.json` |
| 实时更新 | WebSocket 推送；也可点「立即刷新」 |
| 只盯手机 | `WATCH_MACS` 过滤；多路由用 `ROUTER_MACS` |

---

## 原理（一句话）

```
本机 Docker
  → 定时带 WSKEY 调 gw.smart.jd.com
  → listAllUserDevices（你的路由器列表）
  → controlDevice(cmd=get_device_list)（每台路由下的客户端）
  → 对比上次快照，写 online/offline 事件
  → 浏览器 WebSocket 收表表格
```

**不需要**路由器开端口、不需要和路由同一局域网。

API 签名方式参考开源项目：

- [leifengwl/JDRouterPush](https://github.com/leifengwl/JDRouterPush)（`wskey` + `get_device_list`）
- [myleo1/JDC-Monitor](https://github.com/myleo1/JDC-Monitor)（`tgt`/`wskey` 同源）

本项目把「积分推送」改成「**客户端上下线时间线 + 网页表**」。

---

## 准备：拿到 WSKEY（必做）

`WSKEY` 与抓包里的 `tgt` 同值。重登 APP 会失效，需重抓。

### 方法 A：HttpCanary（Android，推荐）

1. 安装 HttpCanary（或 Stream 等抓包）
2. 开始抓包 → 打开 **京东云无线宝** → 点进「积分」或设备详情（触发云请求）
3. 搜索 `wskey` 或主机 `gw.smart.jd.com` / `router-app-api.jdcloud.com`
4. 请求头里复制 **`wskey`** 整串（很长）

图示步骤见 JDRouterPush 文档：  
https://github.com/leifengwl/JDRouterPush#使用说明

### 方法 B：Root 读本地文件

路径（可能随版本变）：

```text
/data/data/com.jdcloud.mt.smartrouter/shared_prefs/jdc_mt_secured_store.xml
```

找 `wskey` 字段值。

### 方法 C：Charles / mitmproxy（电脑）

手机代理到电脑，装证书，过滤 `smart.jd.com`，同样抄请求头 `wskey`/`tgt`。

> 不要把 `WSKEY` 提交到 Git、不要发群。本仓库 `.gitignore` 已忽略 `.env` 与 `data/*`。

---

## 一分钟启动（Docker）

```bash
cd /root/jdc-client-monitor   # 或你 clone 后的目录

docker compose up -d --build

# 浏览器打开
# http://localhost:8780
# 点右上角「设置」→ 粘贴 WSKEY → 保存（可勾选立即轮询）
```

### 网页配置 WSKEY（推荐）

1. 打开 `http://<IP>:8780`
2. 未配置时会自动弹出 **设置**；也可点右上角 **设置**
3. 粘贴 `WSKEY`（留空=不改；勾选「清空」可删除）
4. 可改：轮询间隔、离线防抖、WATCH_MACS、ROUTER_MACS、代理
5. 保存写入 `data/secrets.json`（权限 600），**立刻生效，不用重启容器**
6. 页面只显示脱敏 `前6…后4`，完整 key 不回显

也可用环境变量 `.env` 的 `WSKEY=`（启动时读入）；网页保存后以 `secrets.json` 为准覆盖。

### 常用命令

```bash
docker compose ps
docker compose logs -f --tail=100
docker compose restart
docker compose down

# 改 .env 后
docker compose up -d
```

数据持久化目录：`./data/`

| 文件 | 作用 |
|------|------|
| `data/monitor.db` | SQLite：设备状态 + 事件 |
| `data/aliases.json` | 自定义名字 |
| `data/secrets.json` | 网页保存的 WSKEY / 轮询参数（勿提交 git） |

---

## 网页使用

1. 打开 `http://<IP>:8780`
2. 顶部：**在线 / 离线 / 总数 / 上次轮询**
3. **设备表**：状态、自定义名、主机名、MAC、IP、路由器、时间戳
4. 点 **改名** → 输入「爸爸手机」→ 保存（立刻刷新表）
5. **上下线记录**：每次 online/offline 一行
6. 右上角 **立即刷新** = 马上打一次云端 API
7. 角标 **WS 实时** = WebSocket 已连，轮询结束自动更新表

### 只监控某几台手机

`.env`：

```env
WATCH_MACS=aa:bb:cc:11:22:33,445566
```

支持完整 MAC 或后 6 位。空 = 记录路由返回的全部客户端。

### 多台京东云路由

默认拉账号下全部路由。只要某几台：

```env
ROUTER_MACS=AA:BB:CC:DD:EE:FF,11:22:33:44:55:66
```

---

## 无 Docker 时（开发）

```bash
cd /root/jdc-client-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export WSKEY='你的wskey'
export DB_PATH=./data/monitor.db
export ALIASES_FILE=./data/aliases.json
export PORT=8780
python -m app.main
```

---

## API（可选）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 网页 |
| GET | `/api/devices` | 设备 JSON |
| GET | `/api/events?limit=200&mac=` | 事件 |
| GET | `/api/stats` | 统计 |
| GET | `/api/aliases` | 别名 |
| POST | `/api/alias` form: mac,name | 设/清名字 |
| GET | `/api/settings` | 公开配置（含脱敏 WSKEY） |
| POST | `/api/settings` | 保存 WSKEY/轮询等（form） |
| POST | `/api/poll` | 立即轮询 |
| WS | `/ws` | 实时推送 snapshot |

---

## 防抖与精度

| 参数 | 默认 | 含义 |
|------|------|------|
| `POLL_INTERVAL` | 45s | 轮询间隔；太小可能触发风控 |
| `OFFLINE_MISSES` | 2 | 连续几次快照都没有才记离线 |

手机休眠可能短暂不在 `get_device_list` 里 → 靠 `OFFLINE_MISSES` 减少误报。  
时间精度 ≈ 轮询间隔，不是秒级探针。

---

## 故障排查

**`WSKEY empty`**  
`.env` 没进容器：确认 `docker-compose.yml` 里 `WSKEY: ${WSKEY}`，且存在 `.env`。

**`listAllUserDevices` / 鉴权失败**  
wskey 过期（重登 APP 会废）。重新抓包。  
时钟不对也会导致签名时间窗问题：容器 `TZ=Asia/Shanghai`，宿主机校时。

**有路由、客户端一直 0**  
1. APP 里确认路由在线  
2. `docker logs` 看 `get_device_list` 报错  
3. 接口字段若改版，把一段脱敏 JSON 留档，改 `app/jdc_api.py` 的 `_extract_clients`

**网页开了但不更新**  
看角标是否「WS 实时」；反代需支持 WebSocket。仍可用「立即刷新」或依赖轮询后整页 F5。

**出网失败**  
设 `HTTPS_PROXY` 环境变量。

---

## 目录结构

```text
jdc-client-monitor/
├── Dockerfile
├── docker-compose.yml
├── env.example
├── requirements.txt
├── README.md
├── app/
│   ├── main.py          # FastAPI + WS
│   ├── config.py
│   ├── jdc_api.py       # 云端签名与接口
│   ├── store.py         # SQLite + 别名
│   └── poller.py        # 后台轮询
├── templates/index.html
├── static/{app.js,style.css}
└── data/                # 运行时生成
```

---

## 安全注意

1. `WSKEY` = 账号会话，等同登录态，勿泄露  
2. 网页默认无密码：仅内网访问，或前面加 Caddy 基本认证 / VPN  
3. 勿把 `.env`、`monitor.db` 推进公开仓库  

---

## 和「本机扫局域网」方案区别

| | 本项目（云 API） | 局域网 ARP/ping |
|--|------------------|-----------------|
| 路由在外地 | ✅ | ❌ 扫不到 |
| 需要 WSKEY | ✅ | ❌ |
| 依赖 APP 接口稳定 | ✅ | ❌ |
| 精度 | 轮询级 | 可更高 |

你的场景：**外地京东云无线宝 + APP 账号** → 用本项目。

---

## License

MIT（自用友好）。接口协议归属京东，本工具仅供个人运维监控。
