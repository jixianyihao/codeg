# AresClaw 看板服务内网交接文档

更新：2026-09-10。交接原因：开发迁移至内网环境继续。本文是唯一入口快照：拿到代码后从第 1 节开始即可复现部署与开发环境；细节契约以 [contracts.md](contracts.md)、设计以 [design.md](design.md)、逐轮实施与验证以 [progress.md](progress.md) 为准。

## 0. 快照

| 项 | 值 |
| --- | --- |
| 仓库/分支 | `https://github.com/jixianyihao/codeg.git` → `dashboard-publishing/impl` |
| 最新提交 | `29fb6def`（docs: MySQL 5.7 适配记录） |
| 基线 | `ae18f33e`（codeg release-v0.28.1）；分支共 18 个提交，全部已推送 |
| 内网取码方式 | 可直连 GitHub 则直接 clone；隔离网可用 `git bundle create dashboard.bundle dashboard-publishing/impl`（在能联网机器执行）后在内网 `git clone dashboard.bundle -b dashboard-publishing/impl` |
| 当前完成度 | 服务/CLI/前端全功能可用；本地闭环验证齐全；**真实内网 W3 与 S3 未接入**（见 §9） |

工作树路径（原开发机）：`C:/Users/ouyan/Documents/code/acpdev/codeg/.worktrees.local/dashboard-publishing`。内网机器无需保留该路径结构，直接用分支即可（`services/`、`integrations/`、`docs/`、`src/` 都在分支根下）。

## 1. 组件架构

```
AresClaw 网页(:7000, codeg-server + nginx 静态导出)
  └─ 看板页签：卡片列表（GET /dashboard-api/v1/* 反代→控制域）+ 卡片内管理面板
       │ 点"打开"（新标签页）
       ▼
控制域 (dashboard-service control, :8080)
  ├─ /dashboards/{id}            稳定入口：签发 60s 查看能力→同标签页跳内容域
  ├─ /dashboards/{id}/manage、/  兼容提示页（管理已移入 AresClaw 列表）
  └─ /api/v1/*                   全部业务 API（认证：W3 human / 服务 JWT）
       │ location.replace（单层，控制域无 iframe）
       ▼
内容域 (dashboard-service content, :8081)
  ├─ /view/{id}                  可信加载页：清 fragment→取 /content→唯一 sandbox iframe
  ├─ /render                     旧兼容入口（独立单层）
  └─ /content                    仅接受查看能力 Bearer，text/plain 原文
       ▼
MySQL 5.7+（元数据/ACL/operation/审计） + 私有 S3（不可变 HTML 对象，Key 含 attempt，条件 PUT）
```

三条身份链路：
1. **对话 agent（human）**：平台写好的 W3 Token 文件（生产 `/root/.config/auth_token`）→ Skill+CLI 直连公共 API，`X-Dashboard-Auth-Mode: human`；
2. **机机集成**：运维签发服务 JWT（`dashboard_service.operator` CLI），独立 token 文件，`--auth-mode integration`，两分支互不回退；
3. **浏览器**：访问者自己的 W3 登录（OAuth2 introspection 契约，见 §9 待接入项），浏览器不读 Token 文件。

代码位置：服务 `services/dashboard-service/dashboard_service/`（FastAPI + SQLAlchemy + PyMySQL + Alembic + PyJWT + Boto3）；CLI/Skill 源 `integrations/aresclaw-dashboard/`（无第三方依赖 Python）；AresClaw 前端改动 `src/components/dashboards/`、`src/lib/dashboard-*.ts`、`src/i18n/`、`src/contexts/workbench-route-context.tsx`。

## 2. 数据库（MySQL 5.7 为部署下限）

- 迁移链：`4b58c2e3ae59`（初始）→ `b41d7c2f9013`（S3/attempt 预留）→ `c72a913d8e04`（草稿生命周期）。三步在 5.7.44 与 8.4.6 均实测通过。
- 5.7 适配要点（提交 `383d604a`）：排序规则 `utf8mb4_unicode_ci`（勿改回 `utf8mb4_0900_as_ci`，5.7 建表即失败）；连接字符集由 `create_db_engine` 固定 utf8mb4；恢复扫描 <8.0 无 SKIP LOCKED 自动降级；草稿迁移的 CHECK 门控在 <8.0.16 跳过。
- 5.7 已知代价：CHECK 约束不强制（仅唯一键/外键兜底），应用层校验覆盖所有写入路径——**不要绕过服务直接写库**。
- 隔离 READ COMMITTED；发布结算走"幂等键/配额→暂存校验→S3 条件 PUT+回读→MySQL 终事务"，清理用 IfNoneMatch 墓碑关闭 attempt key（防晚到 PUT）。

## 3. 部署

文件全在 `services/dashboard-service/`：`Dockerfile`（一镜像双入口）、`compose.yaml`、`.env.example`（全部变量模板）、`README.md`（含 nginx 反代样例、恢复流程）、`alembic/`。

```bash
cd services/dashboard-service
cp .env.example .env                    # 填内网 MySQL5.7/S3/双Origin/W3/JWT密钥文件路径
docker compose run --rm migrate         # alembic upgrade head（独立步骤，服务启动校验版本）
docker compose up -d control content    # :8080 控制 / :8081 内容
```

必配变量速记（值不写入仓库）：`DASHBOARD_DATABASE_URL`、`DASHBOARD_JWT_KEY_HOST_FILE`(≥32字节随机)、`DASHBOARD_CONTROL_ORIGIN`/`DASHBOARD_CONTENT_ORIGIN`（必须不同 Origin，非 loopback 需 HTTPS）、`DASHBOARD_ARESCLAW_ORIGIN`（manage 提示页回链）、S3 五项（非 loopback 端点必须 HTTPS）、`DASHBOARD_W3_VERIFY_URL`。

运维命令（宿主机直跑，不走 API）：`python -m dashboard_service.operator create-account/issue/disable-account/set-scopes/recover-operations/cleanup-orphan-files/verify-storage ...`。

## 4. 核心业务语义（接手必读）

- **草稿生命周期**：`draft → published → archived`；`restore` 回到 **draft**（不自动上线，需显式 publish）。看板双指针 current/draft；`save` 只动 draft 指针；version-id 式 publish 只接受**当前 draft_version_id**；rollback 只能回到**曾发布**版本且不新增版本号。
- **已知边角（有意保留，改前先想清楚）**：restore 会把旧线上版本立为草稿候选，随后的文件式发布不清该草稿——形成 current=新/draft=旧 的陈旧草稿，再按 version-id 发布它等效回滚线上。UI 已显示 has_draft，SKILL 要求精确 version-id。
- **列表公开**（2026-09-09 决策）：已发布元数据对已登录 human 全量可见（无授权 role=null，点进才校验 404）；机机列表仍按授权过滤；draft 列表 owner∪editor；archived 仅 owner。
- **幂等**：同键+同指纹重放原结果；不同指纹 409；失败/超时用原 request-id 查询恢复，禁止换键。CLI 写操作全部先 `new-request-id`。
- **退出码**（CLI）：0 成功/2 输入/3 认证/4 无权或不可见/5 冲突/6 处理中/7 未知/8 网络/9 其他。已知文档级差异：HTTP 错误按状态映射、HTTP 200+state=failed 按 code 映射，同一 code 两条路径可能不同值（如 409+invalid_input→5）。

## 5. 测试与验证现状

| 套件 | 命令（`services/dashboard-service/` 下） | 当前结果 |
| --- | --- | --- |
| 服务集成（真实 MySQL+MinIO） | `TEST_DATABASE_URL=... TEST_S3_ENDPOINT_URL=... TEST_S3_BUCKET=... pytest tests -q` | 150 通过（5.7.44 与 8.4.6 双实例） |
| CLI | `python -m unittest discover -s integrations/aresclaw-dashboard/tests` | 32 通过 |
| 浏览器（真实 CSP，Chromium CDP） | `node --test web/tests/browser.test.mjs`（需 `DASHBOARD_TEST_BROWSER`） | 10 通过 |
| 前端 | `pnpm test` / `pnpm build`（构建前设 `NEXT_PUBLIC_DASHBOARD_CONTROL_ORIGIN`） | 4856 通过 / 静态导出成功 |
| 干净安装 | 新 venv + `pip install -r requirements.txt` + 空库 `alembic upgrade head` | 通过（含 alembic 依赖声明） |

跳过即失败：DB/S3 不可用时套件会 skip，必须报告实际执行数。4 个并行 agent 的 Skill/CLI 端到端实测（生命周期/错误边界/跨身份授权/状态机）结论存 [progress.md](progress.md)。

## 6. 对话接入（Skill + CLI）

- 源在 `integrations/aresclaw-dashboard/`（SKILL.md、scripts/dashboard_cli.py、README.md）。
- 安装：复制到 agent 可发现目录（项目级 `.claude/skills/aresclaw-dashboard/` 或用户级 `~/.claude/skills/aresclaw-dashboard/`）。生产执行机：环境已有 `ARESCLAW_DASHBOARD_SERVICE_URL` + `/root/.config/auth_token`，CLI 默认 human 模式直接可用（`scripts/dashboard` wrapper）。
- 开发机 wrapper 会注入本地服务地址与默认 dev 凭据；Token 文件路径必须绝对路径。
- dev 环境便利项（**仅 dev**，`DASHBOARD_DEV_LOGIN=1` 且 loopback 才生效）：`web/auth-provider.dev.js` 默认会话 `dev-alice`（免登录直达），显式登录切换身份存 localStorage（同源各标签共享）；生产用 fail-closed 桩，接真实 W3 浏览器适配器（替换 `web/auth-provider.js`，实现 `getAccessToken`/`login`）。

## 7. 原开发机遗留环境（迁走后处置参考）

| 资源 | 位置/端口 | 说明 |
| --- | --- | --- |
| MySQL 8.4.6 | `.aresclaw-build/mysql/`，13306，库 aresclaw_dash_test | demo 栈数据（约 10 个测试看板） |
| MySQL 5.7.44 | `.aresclaw-build/mysql/mysql-5.7.44-winx64`，data57，13307 | 5.7 验证实例，可保留复测 |
| MinIO | `.aresclaw-build/minio/`，19000 | 曾被 Windows usage-cache 权限问题打挂过，重启即可 |
| demo 看板栈 | 18080/18081/18090（start-dashboard.sh） | 新代码 + dev 登录；`.pid` 文件为准 |
| AresClaw 入口 | nginx 7000 / codeg-server 7100 | 静态导出来自工作树 `out/` |
| 8080/8081/8090 | — | 用户业务保留端口，勿占 |
| e2e-agent*-ws | `Documents/code/e2e-*` | agent 实测工作区，可清理 |

## 8. 禁止事项（架构红线）

不恢复会话桥/SessionRegistry/Token 注入/ACP 钩子/`dashboard_list` Rust handler；不引入 MCP 对话接入；公共服务不用 Rust/SQLite、不与 AresClaw 共库；不给用户 HTML 开放网络/下载/弹窗权限、无通用 postMessage 桥（现有内链跳转白名单机制除外）；不新增永久删除 UI；human/service 认证失败不互相回退；Token 不进命令行/日志/快照。

## 9. 内网待办与验收清单

1. **真实 W3/OAuth2 接入**（最优先）：把 `HttpW3Verifier` 指向真实校验端点（introspection 风格：`active/issuer/user_id/display_name/expires_at/session_ref`；字段名不齐就改适配器映射，别改业务层）；确认稳定企业 UID→principal 映射、登录回跳、注销/撤销、SSO Cookie 的 Domain 是否覆盖内容域子域（不覆盖则内容域独立登录策略需评审）。
2. 浏览器适配器：替换 `web/auth-provider.js` 为真实 W3 登录实现。
3. 生产 S3：TLS、寻址风格（path/virtual）、VersionId 语义、服务端加密；S3 故障语义已脱敏。
4. Token 文件真实格式与多用户执行环境隔离实测（本地用的是受控假 Token）。
5. 反向代理实配：AresClaw `/dashboard-api/v1/*` → 控制 `/api/v1/*`（保留 Authorization）；nginx 双 Origin 与 TLS。
6. Firefox 验证（原机器无 Firefox，未测）；桌面 GPU 环境性能实测（现有 1/5/10MiB 数据见 progress）。
7. 备份/恢复演练（破坏性 downgrade 不是生产恢复方案）；容量与告警。
8. 迁移演练：在 5.7 生产库从空库跑三步迁移 + `verify_schema`（已有干净安装流程可照抄）。

## 10. 文档地图

- [README.md](README.md)：服务部署与运维入口
- [contracts.md](contracts.md)：HTTP/CLI/数据契约（含 MySQL 5.7 边界注记）
- [design.md](design.md)：系统设计（身份、查看隔离、状态机）
- [progress.md](progress.md)：逐轮实施、审查与验证记录（含历史 R1–R15 对照）
- 本文件：交接快照，随交接完成可归档
