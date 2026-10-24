# 积分商城多智能体客服 AI 服务

面向积分商城的多智能体客服应用，采用 **主控 Agent + 六个领域专家 + 统一执行控制**。使用 Python 3.12、FastAPI、LangChain / LangGraph、Pydantic 和 PostgreSQL。

本仓库包含 AI 服务源码、自动化测试、场景测试脚本和技术文档。Ecommerce 业务后端、Customer Service 和前端是独立项目，不包含在本仓库中；完整业务联调需要配套服务或实现同等接口的后端。

## 核心能力

- 主控通过委派工具调用商品、订单、物流、售后、积分、政策六个子智能体。
- 独立子任务异步并行执行；默认并发上限 3，依赖任务按顺序执行。
- 领域工具权限、请求级调用预算、整体超时、相同委派去重和任务回收。
- 结构化领域输出、工具证据引用、业务对象匹配及积分结果字段校验。
- 按主控／子智能体记录模型调用时间、输入输出 Token，以及业务工具耗时和错误码。
- 支持 OpenAI 兼容接口配置；示例使用 OpenRouter，实际模型可在 `.env` 中调整。

积分专家的四个工具：

| 工具 | 职责 |
| --- | --- |
| `get_points_account` | 账户余额、冻结、到期概况及按需查询到期批次 |
| `get_points_transactions` | 积分收支与变动流水 |
| `check_points_eligibility` | 商品兑换资格与不满足条件的原因 |
| `get_order_points` | 订单扣分、应返还与实际返还状态 |

## 目录

```text
atguigu/
  agent/
    factory.py                 # 主控和领域专家构建
    multi_agent/               # 委派、并发、预算和领域结果校验
    harness/                   # 上下文、业务工具、证据、校验和调用指标
    llm/                       # 模型适配及输出定义
  app/                         # FastAPI 接口与应用服务
  infrastructure/              # 数据库及业务 HTTP 客户端
  models/                      # Run、工具调用等审计模型
tests/                         # 单元、图执行和跨服务契约测试
scripts/                       # 客服 20 问测试客户端
docs/                          # 架构介绍、接入协议和测试说明
deploy/local/                  # 原配套项目的本地联调辅助脚本
deploy/staging/                # Ecommerce 演示初始化模块的暂存副本
```

## 安装与启动

以下为 PowerShell 示例，在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
```

编辑 `.env`，填写自己的模型 API Key、数据库连接、内部令牌和 JWT 密钥。调用方、AI 服务和 Ecommerce 的 JWT 配置需要一致。`LLM_MODEL` 使用模型平台提供的完整编号；通用 OpenAI 兼容分支不会自动保证关闭每种供应商的推理模式。

准备可访问的 PostgreSQL 数据库 `ai_customer_service`，再执行：

```powershell
.\.venv\Scripts\python.exe -m atguigu.infrastructure.init_db
.\.venv\Scripts\python.exe -m atguigu.main
```

接口文档：<http://127.0.0.1:8002/docs>。业务请求同时受内部令牌和用户 JWT 校验，具体以 OpenAPI 和路由代码为准。

只运行 AI 服务即可查看接口文档；真实业务查询还需要 `ECOMMERCE_BASE_URL` 对应服务可用、用户身份有效，并具备对应业务数据。

## 自动化测试

填写 `.env` 后执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

基础测试使用脚本模型或模拟工具。跨服务测试需要原目录布局下的 Ecommerce 源码，缺少配套项目时相应用例会跳过；不要将跳过视为真实后端已验证。

2026-10-24 独立发布目录验证：共发现 133 项测试，106 项通过，27 项因缺少配套 Ecommerce 源码跳过；未调用真实模型。

`scripts/test_customer_scenarios.py` 调用真实 FastAPI 场景测试接口，会消耗模型 Token 并记录运行审计。执行前需要启动配套业务服务及 AI 服务，先运行 `--help` 查看参数。

## 业务接口与本地部署说明

业务工具通过 HTTP 调用 Ecommerce，字段与账户隔离约束见[积分客服接入说明](docs/积分客服接入说明.md)。

`deploy/local/configure.py`、`start-services.ps1` 和 `check.py` 服务于原来的多项目目录布局，不能在只克隆本仓库后直接完成全部服务部署。独立使用本仓库时，请按上述步骤手动配置 AI 服务。原配套部署方式见[本地 Docker 开发环境](docs/本地Docker开发环境.md)。

`deploy/staging/refund_expiry_seed.py` 是供 Ecommerce 使用的演示初始化模块副本，需要放入该后端的 `ecommerce_service` 包；它不是 AI 服务的启动步骤。

## 技术文档

- [智能体应用架构与面试介绍](docs/智能体应用架构面试介绍.md)
- [多智能体改造规划](docs/多智能体改造规划.md)
- [积分客服接入说明](docs/积分客服接入说明.md)
- [子智能体并行优化说明](docs/子智能体并行优化说明.md)
- [客服 20 问测试说明](docs/客服20问测试说明.md)
- [历史测试结果与已发现的问题](docs/客服20问测试结果-2026-10-24.md)

文档保留历史验证记录，其中的本机备份、运行报告和同级项目链接属于原联调环境，不随源码发布。

当前领域指引在构建专家时固定注入；运行记录持久化不等同于 LangGraph 检查点恢复。复杂问题的路由和必要查询仍有待优化，历史模型调用失败不能因补充演示数据就视为修复。

本仓库仅提供配置模板，不包含真实 `.env`、模型密钥、数据库备份、运行日志或个人面试复盘。
