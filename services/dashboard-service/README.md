# AresClaw Dashboard Service

设计、接口契约与当前验证记录统一维护在[看板文档入口](../../docs/aresclaw-dashboard/README.md)。

独立部署的公共看板服务：Python 3.11+ / FastAPI / SQLAlchemy / PyMySQL /
MySQL 8 / PyJWT / Alembic / Boto3(S3)。已发布 HTML 存私有 S3，MySQL 保存
元数据、ACL、版本引用与操作记录。AresClaw 网页列表内提供管理面板，
业务权限、发布事务和存储仍由本服务执行；不新增 AresClaw Rust 代理。

## 查看、管理与更新

稳定查看链接 `/dashboards/{id}` 在认证后进入独立内容域的单层沙箱查看页。
管理直接在 AresClaw 列表侧边面板完成；旧 `/dashboards/{id}/manage` 仅提供
返回 AresClaw 的兼容入口。配置 `DASHBOARD_ARESCLAW_ORIGIN` 为既有网页 Origin，
不包含路径或用户可控回跳参数。未配置时明确提示通过 AresClaw/CLI 管理。

看板有草稿、已发布、已下架三态，并分别保存线上和待发布草稿引用：
保存草稿不影响线上；发布指定草稿才切换稳定链接；上传并发布可一次调用完成。
下架后的恢复只回草稿，不自动开放原受众。未发布版本及其历史只允许所有者/
编辑者访问；普通查看者只能访问曾发布版本。所有版本仍计入配额。

本次迁移新增草稿引用与版本首次发布时间；升级保留原有发布版本。
含草稿数据时禁止直接降级为旧服务，否则旧历史接口会失去草稿隐私边界。
请按备份恢复流程处理，不将 schema downgrade 当生产回滚。

## 运行

```sh
cp .env.example .env        # 填入 MySQL、可达的 TLS S3 端点及其他配置
docker compose run --rm migrate        # alembic upgrade head（独立步骤）
docker compose up -d control content   # 两个进程：控制域 :8080 / 内容域 :8081
curl -s localhost:8080/health/ready    # database + s3 + human_auth 检查
```

启动前生成至少 32 随机字节的 JWT 密钥文件（不要提交），将宿主机路径放入
`DASHBOARD_JWT_KEY_HOST_FILE`。Compose 把它挂到三个入口需要的
`/run/secrets/dashboard-jwt.key`；裸机由进程管理器挂载同等受控文件。
MySQL 业务库、私有 S3 Bucket 和服务端凭据也须提前配置；开发 MinIO 的容器
启动本身不会创建业务 Bucket。测试与演示业务必须使用不同数据库/Bucket。

Compose 内的 MinIO 只提供开发存储进程，没有配置 TLS。容器间的
`http://minio:9000` 不满足服务对非 loopback S3 地址的 HTTPS 要求；容器部署
须为 MinIO 配置可信 TLS 入口，或接入已有 HTTPS S3 服务。另一种本地验证
方式是裸机运行控制/内容服务，以 `http://127.0.0.1:9000` 访问映射出来的
MinIO 端口。不要用 `localhost` 让控制服务容器误连自身。

裸机运行（不经 Docker）：

```sh
python -m pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)    # 或用进程管理器注入
alembic upgrade head
python -m dashboard_service serve-control --port 8080 &
python -m dashboard_service serve-content --port 8081 &
```

启动校验：schema 未迁移、S3 不可达、控制/内容 Origin 相同、JWT 密钥
不足 32 字节时拒绝就绪；不会自动建表。

## 运维命令（宿主机执行）

```sh
python -m dashboard_service.operator create-account --account ci-a --scopes read,write
python -m dashboard_service.operator issue        --account ci-a --ttl 2592000
python -m dashboard_service.operator set-scopes   --account ci-a --scopes read
python -m dashboard_service.operator disable-account --account ci-a
python -m dashboard_service.operator enable-account  --account ci-a   # 旧 JWT 不复活
python -m dashboard_service.operator reset-tokens    --account ci-a
python -m dashboard_service.operator list-accounts
python -m dashboard_service.operator recover-operations   # 过期租约→可重试失败
python -m dashboard_service.operator cleanup-orphan-files # S3 孤立对象精确清理
python -m dashboard_service.operator verify-storage       # 全部已提交对象核对
python -m dashboard_service.operator stats
```

签发的 JWT 只打印一次，不进日志/审计/operation 结果。

## 测试（要求真实 MySQL 与真实 S3 端点）

```sh
export TEST_DATABASE_URL='mysql+pymysql://user:pass@127.0.0.1:3306/aresclaw_dash_test'
export TEST_S3_ENDPOINT_URL='http://127.0.0.1:19000'   # MinIO 等真实 S3 API
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
python -m pytest tests -q
```

未配置时测试显式跳过并报告，不用 SQLite/mock 代替。

## 反向代理（示例）

控制域与内容域必须是不同 Origin；AresClaw 同源代理映射列表及管理 API：

```nginx
# aresclaw.example.internal
location /dashboard-api/v1/ {
    client_max_body_size 11m;  # 单 HTML 10 MiB，另留 multipart 元数据开销
    proxy_pass https://boards.example.internal/api/v1/;
    proxy_set_header Authorization $http_authorization;  # 原样转发网页凭据
    proxy_set_header X-Dashboard-Auth-Mode $http_x_dashboard_auth_mode;
}
# boards.example.internal  → control:8080
# board-content.example.net → content:8081（只放行 /view/* /render /render.js /content /health）
```

不把 S3 bucket 映射为静态站点；浏览器永远只访问内容进程。
内容 Origin 不应收到既有 W3 登录 Cookie；使用子域时核实 Cookie Domain，
独立注册域可以避免继承管理域 Cookie。管理前端不嵌入用户 HTML。

本轮真实 S3 协议验证使用未启用 Bucket Versioning 的独立 MinIO。
当前 PUT/孤儿清理尚未完整处理物理 VersionId；开启 Versioning 的 Bucket
不能直接沿用该回收保证。生产先使用专用、未启用 Versioning 的私有 Bucket，
或先完成版本化存储适配与验收。Bucket Versioning 不替代备份。

## 故障与恢复要点

- 发布顺序固定：占位/配额预留 → S3 条件 PUT + 回读校验 → MySQL 最终事务；
  事务失败只产生可精确清理的孤立对象（reservation 状态 cleanup_pending）。
- `recover-operations` 把过期租约标记为 `operation_interrupted`（可同键重试）；
  同键同摘要重试生成新 attempt、新 S3 key，旧 attempt 对象由清理删除。
- S3 故障统一表现为脱敏 `503 storage_unavailable`，不回退本地文件。
- 备份：MySQL 一致性备份 + 版本对象 manifest（bucket/key/VersionId/sha256/
  byte_size）；恢复时先隔离（DASHBOARD_RECOVERY_MODE=1）、更换 JWT 密钥、
  核对 ACL 与对象清单，再放流量。
