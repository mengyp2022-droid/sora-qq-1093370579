# sora注册机qq群：1093370579

一个面向本地部署的 Sora / ChatGPT 账号管理面板，包含协议注册调度、账号资源池管理、手机号绑定、Sora 视频生成和 API Key 池化调用能力。项目采用 `FastAPI + SQLite + 原生前端` 结构，默认通过 Web 控制台完成日常操作。

**QQ 群：1093370579**

![QQ群二维码](docs/qq-group-1093370579.png)

---

## 项目概览

- **协议注册调度**：从邮箱池中提取未注册邮箱，多线程执行注册任务，支持重试、心跳检测、运行日志和停止控制。
- **账号管理**：查看注册结果、Sora 状态、手机绑定状态、额度状态，支持导出账号 CSV 和挑选下一个可用 Sora 账号。
- **邮箱资源池**：支持单条添加、批量导入导出、查看详情，并结合邮箱 API 拉取验证码邮件。
- **手机号资源池**：支持手工录入、批量导入、查询短信验证码、销毁号码，并对接 Hero-SMS 余额与号码获取接口。
- **银行卡资源池**：支持单条添加、批量导入删除，并通过系统设置管理卡平台和使用次数限制。
- **Sora 视频生成**：支持文生视频和图生视频，支持旧链路与 NF2 / 官方 Sora 链路切换、任务并行轮询、状态追踪和自动轮换账号。
- **Sora API Key**：支持为单账号或自动轮换池生成调用 Key，便于将账号能力封装成统一接口。
- **系统设置**：集中配置邮箱 API、短信 API、代理、打码 API、银行卡 API、OAuth 参数、线程数、重试次数，以及后台登录账号密码。

## 技术栈

- 后端：`FastAPI`、`SQLite`、`python-jose`、`passlib`
- 前端：原生 `HTML / CSS / JavaScript`
- 协议层：`requests`、`curl_cffi`，通过纯 HTTP 逻辑执行注册、Sora 激活、手机号绑定与视频请求

## 项目结构

```text
.
├── protocol_register.py          # 协议注册主流程
├── protocol_sora_phone.py        # Sora 激活、手机号绑定、Sora 相关 HTTP 能力
├── protocol_sentinel.py          # Sentinel 相关逻辑
├── main_protocol.py              # 协议批量任务入口
├── web/
│   ├── run_web.py                # Web 管理端启动入口
│   ├── frontend/                 # 前端页面
│   └── backend/app/              # FastAPI 后端
├── docs/                         # 补充说明文档
└── data/                         # 默认数据目录（SQLite）
```

## 快速开始

### 1. 安装 Web 后端依赖

```bash
pip install -r web/backend/requirements.txt
```

### 2. 启动管理后台

```bash
python web/run_web.py
```

启动后访问 [http://127.0.0.1:1989](http://127.0.0.1:1989)。

默认登录账号：

- 用户名：`admin`
- 密码：`admin123`

首次部署后请尽快修改登录密码和 `SECRET_KEY`。

## Docker 启动

项目内已提供 [web/docker-compose.yml](web/docker-compose.yml)：

```bash
docker compose -f web/docker-compose.yml up -d --build
```

默认映射端口仍为 `1989`，数据目录挂载到容器内 `/data`。

## 配置说明

### 环境变量

- `ADMIN_USERNAME`：后台默认管理员账号
- `ADMIN_PASSWORD`：后台默认管理员密码
- `SECRET_KEY`：JWT 签名密钥
- `DATA_DIR`：数据目录，默认指向仓库下的 `data/`
- `CORS_ORIGINS`：跨域来源，默认 `*`

### 后台系统设置

系统设置页会把配置写入 SQLite 中的 `system_settings` 表，主要包括：

- `email_api_url`、`email_api_key`、`email_api_default_type`
- `sms_api_url`、`sms_api_key`、`sms_openai_service`、`sms_max_price`
- `bank_card_api_url`、`bank_card_api_key`、`bank_card_api_platform`
- `captcha_api_url`、`captcha_api_key`
- `proxy_url`、`proxy_api_url`
- `oauth_client_id`、`oauth_redirect_uri`
- `thread_count`、`retry_count`
- `card_use_limit`、`phone_bind_limit`

## 推荐使用流程

1. 启动后台并登录。
2. 在“系统设置”中填写邮箱 API、短信 API、代理、打码和 OAuth 参数。
3. 在“邮箱管理”中导入待注册邮箱资源。
4. 在“批量注册”页启动注册任务，等待结果写入“账号管理”。
5. 如需补绑手机号，使用“手机号管理”与“开始绑定手机”任务。
6. 如需生成视频或对外提供统一调用入口，使用“视频生成”和“Key 管理”页。

## 数据与存储

- 默认数据库文件为 [data/admin.db](data/admin.db)
- 运行日志写入 `run_logs`
- 账号、邮箱、手机号、银行卡、Sora Key、视频任务等都保存在同一个 SQLite 库中

## 文档

- [docs/SORA_ACTIVATION_AND_PHONE_BIND_ANALYSIS.md](docs/SORA_ACTIVATION_AND_PHONE_BIND_ANALYSIS.md)
- [docs/SORA_API_KEY_CALL_GUIDE_CN.md](docs/SORA_API_KEY_CALL_GUIDE_CN.md)
- [docs/SORA_POOL_API_KEY_USAGE.md](docs/SORA_POOL_API_KEY_USAGE.md)

## 说明

- `web/backend/requirements.txt` 只覆盖 Web 管理端依赖。
- 协议注册、Sora 激活和视频调用脚本还依赖 `requests`、`curl_cffi` 等库；如果你要实际使用这些能力，需要把协议层依赖一并安装完整。
- `web/run_web.py` 默认使用 `reload=True` 启动，适合本地开发和调试。

## 开源与免责

本项目仅供技术研究与学习使用，使用前请自行评估相关服务条款、账号风险和当地法律责任。

**QQ 群：1093370579**
