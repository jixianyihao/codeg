# AresClaw Dashboard Service

独立部署的公共看板服务：Python 3.11+ / FastAPI / SQLAlchemy / PyMySQL /
MySQL 8 / PyJWT / Alembic / Boto3(S3)。已发布 HTML 存私有 S3，MySQL 保存
元数据、ACL、版本引用与操作记录。AresClaw 不承载任何看板业务逻辑。

## 运行

```sh
cp .env.example .env        # 填入内网值；本地可 compose 起 MySQL/MinIO
docker compose run --rm migrate        # alembic upgrade head（独立步骤）
docker compose up -d control content   # 两个进程：控制域 :8080 / 内容域 :8081
curl -s localhost:8080/health/ready    # database + s3 + human_auth 检查
```

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

控制域与内容域必须是不同 Origin；AresClaw 同源代理只映射列表路径：

```nginx
# aresclaw.example.internal
location /dashboard-api/v1/ {
    proxy_pass https://boards.example.internal/api/v1/;
    proxy_set_header Authorization $http_authorization;  # 原样转发网页凭据
}
# boards.example.internal  → control:8080
# board-content.example.net → content:8081（只放行 /render /render.js /content /health）
```

不把 S3 bucket 映射为静态站点；浏览器永远只访问内容进程。

## 故障与恢复要点

- 发布顺序固定：占位/配额预留 → S3 条件 PUT + 回读校验 → MySQL 最终事务；
  事务失败只产生可精确清理的孤立对象（reservation 状态 cleanup_pending）。
- `recover-operations` 把过期租约标记为 `operation_interrupted`（可同键重试）；
  同键同摘要重试生成新 attempt、新 S3 key，旧 attempt 对象由清理删除。
- S3 故障统一表现为脱敏 `503 storage_unavailable`，不回退本地文件。
- 备份：MySQL 一致性备份 + 版本对象 manifest（bucket/key/VersionId/sha256/
  byte_size）；恢复时先隔离（DASHBOARD_RECOVERY_MODE=1）、更换 JWT 密钥、
  核对 ACL 与对象清单，再放流量。
