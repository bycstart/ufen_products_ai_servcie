# 本地 Docker 开发环境

本次采用：PostgreSQL 和 Redis 在 Docker Desktop 中运行，三个 Python API、两个 Worker 和前端在本机运行。原虚拟机配置已备份，模型 API 配置保持不变。

## 1. 本机连接

| 组件 | 地址 | 说明 |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:15432` | 用户 `customer_service`，密码由配置脚本生成 |
| Redis | `127.0.0.1:16379/0` | 本地实时消息 |
| Customer Service | `http://127.0.0.1:8000` | 会话、聊天和人工客服 |
| Ecommerce | `http://127.0.0.1:8001/docs` | 商品、订单、积分接口 |
| AI Service | `http://127.0.0.1:8002/docs` | 多智能体与工具审计 |
| 用户前端 | `http://127.0.0.1:5173` | 通过前端原启动方式运行 |
| 客服前端 | `http://127.0.0.1:5174` | 通过前端原启动方式运行 |

三个数据库分别为 `ecommerce`、`customer_service`、`ai_customer_service`。密码、统一 JWT 和内部令牌保存在 `deploy/local/.env`；业务服务的对应字段已更新，不在文档展示密钥。

## 2. 初次配置及初始化

以下命令均从 AI 服务项目目录执行。本次已经完成这部分操作。

```powershell
# 使用现有开发环境；Redis 客户端是 Customer Service 的既有依赖。
uv pip install --python .\.venv\Scripts\python.exe 'redis>=6.2.0'
.\.venv\Scripts\python.exe deploy/local/configure.py
docker --context desktop-linux compose -f deploy/local/compose.yaml up -d --wait
```

`configure.py` 修改三个服务 `.env` 和两个前端 `.env.local` 前会备份原文；不覆盖原 LLM 参数。重复执行复用本地数据库密码，避免与已有持久化卷失配。切换了 JWT 后需要重新登录。

仅第一次对新本地数据库建表、加入演示数据：

```powershell
$localPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
Push-Location ..\..\backend\backend\ecommerce-service
& $localPython -m ecommerce_service.points_seed
Pop-Location
Push-Location ..\..\customer-service\customer-service
& $localPython -m atguigu.infrastucture.init_db
Pop-Location
& $localPython -m atguigu.infrastructure.init_db
```

积分演示账户 `ACCOUNT_DEMO_001` 初始可用 4000、冻结 200，其中 3000 在初始化后 15 天到期。独立积分订单为 `ORDER_POINTS_DEMO_001`，扣减 2000。原现金订单不变。规则带“演示”标注，不能代表企业正式制度。虚拟机历史订单、积分和聊天记录未自动迁移。

## 3. 日常启动和验证

```powershell
docker --context desktop-linux compose -f deploy/local/compose.yaml up -d --wait
.\.venv\Scripts\python.exe deploy/local/configure.py --check
powershell -NoProfile -ExecutionPolicy Bypass -File deploy/local/start-services.ps1
.\.venv\Scripts\python.exe deploy/local/check.py
```

启动脚本后台运行 Ecommerce、AI、Customer API、AI Worker、Outbox Worker，日志位于 `deploy/local/logs`。启动进程后应运行检查脚本，不能仅凭 PID 判定 API 就绪。

如果希望在 IDE 单独调试 API，可分别运行：

```powershell
.\.venv\Scripts\python.exe deploy/local/serve.py ecommerce
.\.venv\Scripts\python.exe deploy/local/serve.py ai
.\.venv\Scripts\python.exe deploy/local/serve.py customer
```

三个命令分别占用终端；不要与后台脚本重复占用端口。Customer Service 目录下的两个 Worker 仍需分别执行 `python -m atguigu.worker.ai.worker`、`python -m atguigu.worker.realtime`，且解释器需安装项目依赖。本机辅助脚本使用当前 AI `.venv`，三个应用仍在各自工作目录、独立进程中运行。

`configure.py --check` 和本地 API 入口会检查进程环境变量；发现旧数据库地址、JWT 等覆盖值时拒绝继续。`check.py` 还检查实际 AI 引擎 URL，避免把审计记录写进虚拟机。

停止后台脚本启动的 Python 服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy/local/start-services.ps1 -Stop
docker --context desktop-linux compose -f deploy/local/compose.yaml stop
```

停止操作保留两个数据卷。三库初始化 SQL 仅在新 PostgreSQL 卷首次启动时执行，现有数据卷不会重复建库。

前端按各自 `start.ps1` 或 `npm run dev` 启动；新 `.env.local` 指向本机，已运行的 Vite 需重启。

## 4. 验证范围和备份

`check.py` 验证三个 PostgreSQL 库、Redis 临时键读写、AI HTTP、三服务 JWT，以及九个真实工具调用的 HTTP、返回模型、证据和审计入库。它不调用大模型，不发送用户聊天。

2026-10-24 验证结果：两个 Docker 依赖健康、三个 API 和两个 Worker 在运行；真实本地数据库九工具联调通过，全量自动化测试 77 个通过。

回归命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

原配置备份为 `backups/before-local-docker-20261024-153411-346488.zip`，含密钥，仅保留本机。需要恢复虚拟机时先停止本地服务，按压缩包的 `manifest.json` 恢复原先存在的 `.env` 文件；原先不存在的前端 `.env.local` 应删除，再重新启动服务和前端。本地数据库保留，恢复配置不会自动把本地数据同步到虚拟机。

健康检查采用 [Docker Compose 官方文档](https://docs.docker.com/compose/how-tos/startup-order/)说明的方式。Compose 定义见 [compose.yaml](../deploy/local/compose.yaml)。

## 5. 2026-10-24 前端启动与地址修改验证

两个前端已在本机启动并连接本地 API：

- 用户端：<http://127.0.0.1:5173/chat>
- 客服端：<http://127.0.0.1:5174/>
- 地址修改页：<http://127.0.0.1:5173/me/orders/ORDER_001/address>

使用 `user_001` 体验账号，`ORDER_001` 本轮查询状态为 `pending_shipment`，可以修改地址。手动路径为“我的 → ORDER_001 → 修改地址 → 保存地址”。已发货、已完成等状态不可修改。

两个前端构建通过，首页及地址路由 HTTP 200，三个 API 可访问，Customer JWT 与历史接口验证通过。地址 PATCH 使用原地址原值提交并成功读回，原有地址未改变；浏览器来源 `127.0.0.1:5173` 的 PATCH 跨域预检通过。未执行取消订单、退款或其他业务写入。

真实模型生成聊天页面引导本轮未通过：订单专家达到执行限制，未返回地址按钮；模拟图页面引导回归通过不代表真实模型每次都能生成入口。可先直接使用订单页面测试地址保存。实测记录见 `reports/frontend-address-smoke-20261024.json`。

前端进程信息保存在 `deploy/local/frontend-runtime.json`，日志为 `deploy/local/logs/user-frontend.*.log`、`admin-frontend.*.log`；它们独立于 Python 服务的 `runtime.json`。本次采用后台 Vite 启动供本地测试，非公网生产部署。用户端旧锁文件存在依赖版本不一致，安装时使用 `npm install --package-lock=false`，未改写原锁文件；客服端使用 `npm ci`。
