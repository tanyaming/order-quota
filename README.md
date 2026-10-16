# 下单额度管理（Order Quota）

多账号 × 多货号的下单额度记录工具。**单文件静态页面 + 云端数据库，无需任何构建步骤。**

## 线上地址

https://order-quota.app.workbuddy.host/

- 应用 ID（WorkBuddy 应用托管）：`wbapp_c9U187dBqjV5JL5zeCS2mM`
- 入口文件：`index.html`

## 文件说明

| 文件 | 说明 |
|---|---|
| `index.html` | **线上版本**，唯一交付物，部署这个文件即可 |
| `single-device-version.html` | 早期单机版（localStorage，无云端），仅作留档 |

## 角色与权限

| 角色 | 能力 |
|---|---|
| 管理员 | 管理平台账号池、客户账号、客户可用账号范围、按货号自定义额度；查看全部下单明细与当月总览 |
| 客户 | 在可用账号范围内录入 / 修改 / 删除自己的下单记录；查看自己的下单数据；仅看聚合数字 |

## 业务规则

- 额度维度：**账号 × 货号 × 月份**，默认每账号每货号每月 100 单，可按货号单独覆盖
- 货号 = 商品 ID
- 客户可用账号由管理员配置（`全部账号` / `指定账号`），**服务端强制校验**，前端无法绕过
- 配额并发安全：写入前对 `(货号, 账号, 月份)` 加 `pg_advisory_xact_lock`
- 批量写入要么全成功要么全不写

## 云端服务

- **应用托管**：WorkBuddy 应用托管（静态站，前置 EdgeOne CDN + 腾讯云 CLB）
- **数据库**：WorkBuddy Cloud Service 托管 PostgreSQL

业务表：

`orders`、`platform_accounts`、`app_users`、`app_sessions`、`product_limits`、`app_config`、`account_assignments`

接口：通过 PostgREST `rpc` 调用 `SECURITY DEFINER` 函数

| 函数 | 用途 |
|---|---|
| `fn_login(text, text)` | 用户名密码登录，签发会话令牌 |
| `fn_api(text, text, jsonb)` | 业务接口：查单、录入、编辑、删除、总览等 |
| `fn_admin(text, text, jsonb)` | 管理员接口：账号池、客户、可用账号范围、额度 |
| `fn_limits(text)` | 额度字典 |

安全模型：所有业务表启用 RLS 且**不授予 `anon` / `authenticated` 任何权限**，只能经上述函数访问。

认证为自建实现（bcrypt 加密 + `app_sessions` 会话令牌，30 天有效，5 次失败锁定 10 分钟），不依赖平台 Auth。

## 本地开发

无需构建，起一个静态服务即可预览：

```bash
python3 -m http.server 8899 --bind 127.0.0.1
# 打开 http://127.0.0.1:8899/index.html
```

## 发布

在 WorkBuddy 中对该目录执行应用部署（覆盖发布），线上地址不变。

## 注意

- 页面内的 `publishableKey` 是**公开标识**，非密钥，可安全入库
- 管理员密码以 bcrypt 哈希存储在云端数据库，**不在本仓库中**，请另行保管
- 如仓库公开，建议改为 private
