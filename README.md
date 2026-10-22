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

运维表：

`client_events` —— 前端埋点与异常上报，只写不清（见「运维与排障」）

接口：通过 PostgREST `rpc` 调用 `SECURITY DEFINER` 函数

| 函数 | 用途 |
|---|---|
| `fn_login(text, text)` | 用户名密码登录，签发会话令牌 |
| `fn_api(text, text, jsonb)` | 业务接口：查单、录入、编辑、删除、总览等 |
| `fn_admin(text, text, jsonb)` | 管理员接口：账号池、客户、可用账号范围、额度 |
| `fn_limits(text)` | 额度字典 |
| `fn_log(jsonb, text, text)` | 前端埋点写入（匿名可调，带截断与限流） |
| `fn_events(text, int, text)` | 管理员查询埋点（校验 admin 角色） |

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

---

## 运维与排障

### 已知故障模式：站点偶发"任何人都打不开、几分钟自愈"

2026-10-22 17:00 前后发生过一次。**根因在平台侧**（回源容器瞬时不可用），应用本身无问题：

- EdgeOne CDN 对该域名的响应恒为 `eo-cache-status: MISS`、`age: 0`，即**每次访问都直连回源容器**，容器重启那几分钟全网不可用
- 平台不对外提供历史访问日志，所以必须自建留存
- 该故障无法从应用内部修复；能做的只有「记录 + 快速发现 + 缩小影响面」

### 三层可观测性（各管一段，缺一不可）

| 层 | 手段 | 能捕捉什么 | 局限 |
|---|---|---|---|
| 外部探针 | GitHub Actions 每 5 分钟 | **容器挂掉**（页面完全不执行 JS 时唯一有效） | cron 会延迟 5~15 分钟，抓不到精确窗口 |
| 客户端埋点 | `index.html` 上报到 `client_events` | 页面能打开但用不了、SDK 加载失败、API 报错 | 页面没加载出来时无数据 |
| 本机体检 | `tools/healthcheck.sh` | DNS/证书/并发/内容一致性 | 需手动或定时触发 |

**判断故障窗口的方法**：`client_events` 里 `page_load` 的时间序列出现空档，同时 `monitor/incidents.log` 有 `down` 记录，两者交集就是故障区间。

### 1. 本机一键体检

```bash
./tools/healthcheck.sh          # 单次，异常时退出码 1
./tools/healthcheck.sh -w 60    # 每 60 秒巡检
```

检查 6 项：DNS 多线路一致性 / TLS 剩余天数 / 静态页（含与本地 md5 比对）/ 数据平面 / SDK CDN 延迟 / 10 路并发。结果追加到 `logs/healthcheck-YYYYMMDD.log`。

### 2. 客户端埋点

页面自动上报以下事件到 `client_events`：

| kind | 触发时机 |
|---|---|
| `page_load` | 每次成功初始化（含加载耗时、SDK 来源、网络类型） |
| `init_fail` | SDK/初始化失败（`detail.sdk` 为 `none` 表示两个 CDN 都没加载到） |
| `api_error` | 接口返回错误 |
| `api_fail` | 接口网络层失败 |
| `js_error` / `promise_error` | 未捕获异常 |

查最近事件（管理员令牌）：

```sql
SELECT * FROM client_events ORDER BY id DESC LIMIT 50;
```

或调 `fn_events(p_token, p_limit, p_kind)`。

写入侧有限流（每分钟 300 条）与字段截断（单条 detail ≤ 2000 字节），公开 key 被滥用也打不爆库。

### 3. GitHub Actions 探活

`.github/workflows/uptime.yml`，每 5 分钟一次：

- 正常：只写 Job Summary，不产生提交
- 状态变化：写入 `monitor/incidents.log` + `monitor/state.json` 并自动提交
- 转为不可用：自动开 Issue（GitHub 会发邮件通知）
- 恢复：自动在 Issue 下留言并关闭

手动触发：仓库 Actions 页 → `uptime` → Run workflow。

### 前端加固（v1.1.0）

| 改动 | 解决什么 |
|---|---|
| SDK 双源：jsdelivr → unpkg 兜底 | jsdelivr 在国内长期抖动，单源失败即白屏 |
| SDK 版本锁死到 `0.1.4-dev.ca27990.202610161713` | 原先用 `@dev` 浮动标签，上游更新会导致线上静默崩溃 |
| 初始化失败显示「服务暂不可用」+ 自动重试一次 | 原先只有一个干巴巴的「初始化失败」 |
| `pagehide` 时用 `keepalive` 冲刷埋点 | 关页面时事件不丢 |

**升级 SDK 的方法**：改 `index.html` 里两处 `@0.1.4-dev.xxx` 为同一新版本号，先用 `tools/healthcheck.sh` 确认新 URL 可访问，再发布。

### 发布前检查清单

1. `./tools/healthcheck.sh` 全绿
2. 本地起服务，浏览器确认登录页正常渲染
3. 记录当前线上 md5，发布后比对确认内容已更新
4. 发布后 5 分钟内看一次 `client_events` 有无 `page_load` 进来（证明埋点生效）
