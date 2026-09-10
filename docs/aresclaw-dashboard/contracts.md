# AresClaw 看板接口与数据契约

更新：2026-09-09。系统边界见 [design.md](design.md)，实施、审查与测试统一见 [progress.md](progress.md)。基路径为独立服务 `/api/v1`；以下省略该前缀。

## 1. 通用规则

- 认证：`Authorization: Bearer <credential>`；`X-Dashboard-Auth-Mode: human|service` 只选择校验器，默认 human，不提供主体或权限。
- 请求/响应为 JSON；上传为 multipart；时间为带时区 RFC3339，服务内部统一 UTC。
- ID 为 UUID。每个业务写带 UUID `Idempotency-Key`；既有资源写另带 expected_revision 正整数。
- 相同主体、同键、同 method/path/冻结业务参数重放原操作；不同指纹返回409。新意图才使用新键。
- 分页返回 `{items,next_cursor}`，默认20/最大50；每页重新授权，不提供未授权资源总数。
- 看板按 updated_at DESC、id DESC 排序；游标绑定主体/过滤条件，不承诺并发更新下快照分页。
- 无权限资源与不存在资源统一404；不可因掌握版本 ID、operation ID 或 S3 Key 绕过权限。
- JSON 请求体128 KiB；multipart 整体上限为 HTML 限额加256 KiB开销。计实际流入字节，超限只响应一次413。
- 所有动态授权/内容响应 no-store、nosniff、no-referrer；错误脱敏，不含 Token、连接串或存储凭据。

## 2. 返回模型

```typescript
type DashboardStatus = "draft" | "published" | "archived"
type Role = "viewer" | "editor" | "owner" | null
type Page<T> = { items: T[]; next_cursor: string | null }
type Operation<T> = {
  operation_id: string; request_id: string
  state: "accepted" | "processing" | "succeeded" | "failed"
  result: T | null; error: ApiError | null
}
type ApiError = {
  code: string; message: string; retryable: boolean
  trace_id?: string; operation_id?: string
}
type Dashboard = {
  id: string; title: string; description: string
  owner_principal_id: string; owner_name: string; owner_type: "human" | "service"
  status: DashboardStatus; revision: number; role: Role
  current_version_id: string | null; current_version_number: number | null
  current_version_sha256: string | null; current_version_byte_size: number | null
  draft_version_id: string | null; draft_version_number: number | null; has_draft: boolean
  draft_version_sha256: string | null; draft_version_byte_size: number | null
  created_at: string; updated_at: string; published_at: string | null
  expires_at: string | null; view_url: string
}
```

viewer/无权限目录卡片的 draft_version_id/number 为 null、has_draft=false，不暴露真实草稿标识/数量。expires_at 描述调用者当前最高角色的到期，不是全看板到期。

list/show 的版本摘要和大小直接读取 MySQL，不读取 S3 正文。current 与 draft 的 SHA-256 分开表示，按上传 HTML 的原始字节计算，不规范化换行、空白或编码，不使用 S3 ETag。无对应版本为 null；viewer 的 draft 摘要/大小为 null，无内容权限的目录卡片两组摘要/大小均为 null。下架 owner 可读管理摘要，但仍不能下载源码。

版本包含 id、number、sha256、byte_size、created_at、created_by、published_at、is_current、is_draft；published_at 为空表示从未发布。viewer 的历史只包含曾发布版本；owner/editor 可读未发布历史；下架只能 owner 读管理元数据。

内容/状态写结果包含 dashboard_id、revision、status、disposition，以及适用的 version_id/number、双指针、published_at、sha256、稳定 view_url。ACL/群组写按其契约返回资源 ID 与 revision；不要把“保存成功”解释成“已上线”。

## 3. 身份、发现与主体搜索

| 方法/路径 | 请求/响应 |
| --- | --- |
| GET /me | principal_id、principal_type、display_name、scopes、is_admin；身份以验证结果为准 |
| GET /capabilities | api_major、features（包含create_draft/save_draft/publish_draft）、max_upload_bytes、page_size_max、content_origin、auth_methods、server_time |
| GET /principals | type=user/service/group 可省略、q、cursor、limit；已登记主体元数据，非企业全量目录 |

主体目录统一按 display_name、type、id 排序，包含同名项，跨类型分页不丢群组/机器；签名游标绑定主体、类型、搜索条件。搜索最多200字符。

机机 scope 与角色分别校验：read 查询/源码/operation，write 保存/发布/回滚，manage ACL/下架/恢复。owner + write 可首次发布自己的看板；human 使用其验证身份与角色。机器不能以 W3 公开规则获得权限。

## 4. 看板与版本 HTTP API

| 方法/路径 | 参数与行为 |
| --- | --- |
| GET /dashboards | scope=mine/shared/all，status=draft/published/archived，q/cursor/limit；默认published |
| GET /dashboards/{id} | 详情；无访问角色404，即使元数据卡片可发现 |
| POST /dashboards/drafts | JSON title、description?；创建空私有草稿，计数量配额 |
| POST /dashboards | multipart；创建HTML版本，disposition=publish 或 save_draft |
| POST /dashboards/{id}/versions | multipart+expected_revision；保存独立草稿或更新并发布 |
| PATCH /dashboards/{id} | title?、description?、expected_revision；未提供保持原值 |
| GET /dashboards/{id}/versions | cursor/limit；权限过滤后的版本历史 |
| GET /dashboards/{id}/versions/{version_id}/source | 校验角色/版本归属；text/plain 附件原文字节，X-Content-SHA256 返回该版本摘要，X-Dashboard-Version-Id 返回版本ID，不直接执行 |
| POST /dashboards/{id}/publish | version_id、expected_revision；必须仍为当前 draft_version_id |
| POST /dashboards/{id}/rollback | version_id、expected_revision；只能回滚到曾发布版本，状态须published |
| POST /dashboards/{id}/archive | expected_revision；owner 下架，保留引用/ACL |
| POST /dashboards/{id}/restore | expected_revision；owner 将archived恢复draft，不自动上线 |

title 为1–200字符，description 最多2000字符。POST /dashboards 与 /versions 只接受按序的 metadata JSON part、唯一 html file part：

```json
{
  "title": "经营周报",
  "description": "已聚合的静态数据",
  "content_sha256": "<64位小写SHA-256>",
  "byte_size": 12345,
  "disposition": "save_draft",
  "expected_revision": 3
}
```

新建含文件时 title 必填、无 expected_revision；更新时 title/description 可省略。disposition 默认 publish 以兼容已有发布命令；save_draft 与 publish 是不同幂等请求。

单 HTML 非空 UTF-8，最多10 MiB；真实大小与 SHA-256 必须匹配。扩展名/Content-Length/ETag 不能代替验证。不接受额外文件、ZIP、用户指定对象地址或服务器执行代码。

save_draft 仅切 draft 指针，线上旧版继续可用；publish 上传版一次事务切 current，保留不相关 draft。指定草稿发布要校验当前 draft 与 revision，再切 current、清该 draft。失败保持两指针原状。

草稿首次/恢复后上线需要 owner + write；已上线看板允许 editor + write 发布。下架禁止全部新内容/源码/能力请求；恢复候选若已有 draft 则保留，否则使用保留 current。

## 5. ACL 与群组

| 方法/路径 | 请求/行为 |
| --- | --- |
| GET /dashboards/{id}/grants | owner；items与revision；条目可含subject_name用于展示，写入仍以subject_type/subject_id定位 |
| POST /dashboards/{id}/grants | subject_type、subject_id、role、starts_at?、expires_at?、expected_revision；upsert |
| DELETE /dashboards/{id}/grants/{type}/{subject} | expected_revision 查询参数；撤销单规则 |
| PUT /dashboards/{id}/public-access | enabled、starts_at?、expires_at?、expected_revision；公开固定viewer |
| POST /dashboards/{id}/access-changes | expected_revision、changes[]；1–50项原子修改 |
| GET /dashboards/{id}/access | 自查；owner可传subject_id检查目标 |
| GET /groups | q/cursor/limit；群组元数据 |
| POST /groups | human、display_name；创建当前用户拥有的空组 |
| GET /groups/{id} | 群主；详情、members、revision |
| PUT /groups/{id}/members | 群主、members[]、expected_revision；最多1000个已登记human，全量CAS |

subject_type=user/service/group/all_authenticated；公开 subject_id 固定 `*`，角色仅 viewer/editor，owner 不可授予。普通主体必须存在且类型相符。

创建授权省略时间为null；更新已有grant省略端点保留、显式null清除；两端都存在时 starts_at < expires_at。公开设置是完整规则设置，CLI/UI应发送期望完整时间窗口。

access-changes 的 action 为 set_public、grant、revoke；整组失败全部回滚，revision只增一次，不删除未提及规则。“只保留某组”需显式提交其他撤销项。群组成员增删由CLI读一次后冻结完整列表，冲突不强行覆盖。

## 6. 幂等、结果查询与错误

GET /operations/{id} 或 GET /operations?request_id=<uuid> 只查询当前主体的操作；查询仍检查当前资源与草稿版本资格。viewer不能通过旧operation读取未发布版本或关联草稿指针。

accepted/processing 返回202与 Retry-After，不代表成功。租约过期可记录 operation_interrupted/retryable=true；同键同指纹显式重试产生新 attempt，旧 attempt不得提交或覆盖结果。结果默认保留7天，清理后紧凑键记录继续防重复，返回410。

| HTTP | 典型code/含义 |
| --- | --- |
| 400/422 | invalid_input、invalid_time、invalid_html、hash_mismatch；输入不合法 |
| 401 | authentication_required、token_expired、token_revoked；修复同一身份认证 |
| 403/404 | action_forbidden / not_found；动作权限不足或资源不可见 |
| 409 | revision_conflict、idempotency_conflict、operation_lease_lost；核对后处理 |
| 410 | idempotency_result_expired；不自动换键重做 |
| 413/429 | upload_too_large / upload_busy、rate_limited；限额或退避 |
| 503/507 | identity/storage/database_unavailable / quota_exceeded；保持现有内容 |

超时或连接断开时先查询原键；不得自动换键、换身份或声称发布失败/成功。业务写的HTTP200仍须读取operation.state；未知状态视为协议错误。

## 7. 查看 HTTP 与 CSP

| Origin/路由 | 语义 |
| --- | --- |
| 控制 /dashboards/{id} | 稳定分享入口；使用访问者W3，支持 ?version={version_id} |
| 控制 /dashboards/{id}/view | 同一查看流程的兼容入口 |
| 控制 /dashboards/{id}/manage 与 / | 仅兼容说明；可链接配置的AresClaw列表，不含管理表单 |
| 控制 POST /dashboards/{id}/view-capabilities | body={version_id?}；human-only，返回render_url/expires_at，不走通用operation |
| 内容 /view/{id}、/render、/render.js | 可信加载页与兼容入口，不被外部页面嵌入 |
| 内容 GET /content | 仅capability Bearer，返回经过校验的text/plain HTML |

render_url 固定为内容 Origin + /view/{id}#capability；最多60秒并受身份/授权期限限制。浏览器校验目标Origin/路径，清fragment后 credentials:omit、no-store、redirect:error 请求内容；不传播W3/集成JWT。

控制页面 CSP：
```text
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```
内容可信加载页 HTTP CSP：
```text
default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline';
img-src data: blob:; font-src data:; connect-src 'self'; frame-src about:;
object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```
srcdoc 最前插入的 meta CSP：
```text
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data: blob:; font-src data:; media-src 'none'; connect-src 'none'; frame-src 'none';
object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'
```

唯一iframe仅 sandbox=allow-scripts，不含allow-same-origin。内容刷新失去能力时提供固定控制入口重新授权；错误用textContent，不执行用户内容作为可信父页面。

外链点击：渲染包装在HTML脚本之前监听真实click/auxclick，普通HTTP(S) a[href]链接通过内容域专用MessageChannel在新标签页打开。不支持的非空协议链接明确取消默认导航。#锚点在window冒泡阶段处理，尊重报告已有的defaultPrevented，使用当前iframe的location.hash定位而不向父页基址发请求。包装先创建通道、把发送端及原生发送/DOM方法留在闭包，父页只接受当前iframe、opaque origin的首次初始化并绑定转移端口，后续忽略window级打开消息及重新绑定。可信父页只接受该私有端口上的有效HTTP(S) URL（最多4096字符、无username/password）且有浏览器用户激活时才自动打开，固定noopener/noreferrer。错误来源、非法URL和未知消息均忽略，不向iframe传回Token或capability；无管理RPC。有效私有端口请求若激活不可检查或已过期，只展示备用入口，不自动打开。备用入口显示经过校验的目标地址，使用真实a标签由用户打开；不能将window.open的null返回值直接当作失败，因为noopener下可能正常返回null。本轮不增加iframe的allow-popups/allow-top-navigation权限；即使报告有无关点击带来的激活，也不能凭普通消息伪造外链打开。源文件/source接口/存储摘要保持原文语义。

## 8. AresClaw 与 CLI

网页固定代理 `/dashboard-api/v1/* → /api/v1/*`；现有网页认证封装提供当前W3。管理链接为配置的 AresClaw Origin + `/workspace?view=dashboards&dashboard={id}`；Next静态导出使用查询参数。

CLI human 默认每次读取 /root/.config/auth_token；可通过部署的 service_url/workdir/token_file 配置对齐既有环境。integration 使用 `--auth-mode integration --config <file>` 且独立token_file必填；发送service模式头。两模式共享DirectTransport，不回退认证、不接受明文--token、不跟随重定向或环境代理。

| CLI | 参数与行为 |
| --- | --- |
| new-request-id | 本地生成UUID |
| create | --title、--description?、--request-id；空草稿 |
| save | --file、--title?、--description?、--dashboard-id?、--expected-revision?、--request-id；保存草稿 |
| publish --file | 同save参数；上传并发布；新建必须title |
| publish（无file） | --dashboard-id、--version-id、--expected-revision、--request-id全部必填；发布当前指定草稿 |
| list / show / versions | 分页元数据；list/show含线上/草稿版本ID、SHA-256、字节数、revision，不内嵌HTML |
| source <id> | --output必填；--version current（默认）或draft，与--version-id互斥；--expected-sha256可选；按固定版本下载原始字节，校验摘要后新建文件，不覆盖已有文件 |
| rename / rollback / archive / restore | 原命令继续；写入需要request-id/revision，restore只回草稿 |
| principals / grants / share / revoke / public / access / access-apply | 主体查找、授权与批量修改 |
| group list / create / member add或remove | 本地人类群组；成员修改冻结完整CAS请求 |
| operation | --operation-id 或 --request-id；查询原结果 |

输出一个JSON对象到stdout，stderr简短脱敏诊断。退出码：0成功、2输入、3认证、4无权限、5冲突、6处理中、7写结果未知、8读网络失败、9其他服务/协议错误；failed按已知错误类别映射，HTTP200不保证退出0。

source 成功 JSON 含 dashboard_id、version_id、sha256、byte_size、output；通过 current/draft 选择时另含读取详情时的 revision。先读取一次详情、固定 version_id，再下载该版本；期间线上切换不改变下载目标。expected-sha256 必须为64位小写十六进制，不合法时在读取凭据前返回输入错误。响应摘要/版本头、选中元数据或 expected-sha256 不一致时返回 source_integrity_mismatch、退出码9且不创建输出文件或父目录；UUID版本标识按规范化后的身份比较。旧服务缺少摘要头时仍计算并返回实际摘要，可通过 expected-sha256 对照已读元数据。程序从 list 选择版本时，推荐显式 version-id + expected-sha256 保持同一快照。

对比更新：list/show 取得 id、revision、目标版本摘要 → 原始字节计算本地 SHA-256 → 相同内容且无状态/元数据/权限变更时由调用方跳过上传；有差异时可 source 取原文作 diff → save 或 publish --file 携带原 id、原 revision、新 request-id。服务不按摘要自动跳过业务动作；409 后重新核对差异，不自动用新 revision 强制覆盖。上传新增不可变版本，稳定链接切换引用，不覆盖历史 S3 对象。

先校验并规范request UUID，再读取凭据/调用me/访问快照。快照位于工作区 .aresclaw-dashboard/requests/<origin-hash>/<principal_id>/，绑定固定服务与稳定主体；同人续期可重试、不同身份不能重用。

上传冻结原文、元数据、disposition、revision；access-apply和群组计算结果也冻结。源文件修改/删除不影响同键重试；所有路径层级拒绝符号链接/目录联接和越界。保留快照至少覆盖服务结果保留周期，不存凭据。

旧CLI快照与旧发布指纹的兼容必须保留原空description等冻结语义，不能将新的“省略保持”请求与显式清空混为同一请求。未知/202结果保留原请求供查询，客户端不自动报完成。

## 9. MySQL 与 S3

部署下限 MySQL 5.7（8.x 同样支持）：文本排序规则为 `utf8mb4_unicode_ci`，连接字符集由服务端固定为 `utf8mb4`；过期操作恢复在 <8.0 时退化为普通 `FOR UPDATE`（恢复本身持独占守卫）；`CHECK` 约束仅在 8.0.16+ 作为数据库层纵深防御，5.7 下由应用层校验兜底，业务语义不变。

| 表 | 核心字段/约束 |
| --- | --- |
| principals / identity_links | human/service主体；唯一issuer+enterprise_user_id映射 |
| service_accounts | principal_id、name唯一、enabled、token_version、scopes、revision；不存原JWT |
| groups / group_members | 本地群组、群主、revision；group+human联合主键 |
| dashboards | owner、元数据、status、revision、current_version_id、draft_version_id、时间；published_at可空 |
| dashboard_versions | dashboard、number、S3坐标、sha256、byte_size、created_by/at、published_at；对象Key与dashboard+number唯一 |
| dashboard_grants | dashboard+subject_type+subject_id唯一；role与时间窗 |
| operations | principal+idempotency_key唯一；request_hash、attempt、lease、state、result/error、保留时间 |
| upload_reservations / quota_usage | attempt级对象与预留；owner/global字节和数量账本 |
| view_capabilities | token_digest、human、dashboard/version、到期；不存原能力或W3Token |
| authorization_guard / audit_events | 权限变更协调；主体、动作、对象、前后摘要与operation/trace引用 |

双版本指针分别以组合外键确保版本属于同一看板；version保留dashboard外键。DB使用READ COMMITTED、InnoDB、UTC DATETIME(6)。当前schema head为c72a913d8e04；正向迁移回填旧version.published_at=created_at，保留旧指针/状态/S3对象。

reservation使用reserved、cleanup_pending、uploaded、committed、deleted；cleanup_pending兼作PUT已开始/结果未知的持久标记，不能因其名字直接删除。清理还须证明无提交资格，最终行锁确保只释放一次。租约300秒、清理宽限300秒。

S3固定endpoint/region/bucket/prefix，条件PUT后回读SHA-256/大小，唯一Key最长512ASCII字符；上传失败或未知保留对象坐标与预留，不能仅凭HEAD/ETag判断成功。

配置：DASHBOARD_S3_ENDPOINT_URL、DASHBOARD_S3_REGION、DASHBOARD_S3_BUCKET、DASHBOARD_S3_PREFIX、DASHBOARD_S3_ADDRESSING_STYLE；服务端凭据走受控SDK凭据链，不在业务请求中选择endpoint或bucket。

当前只支持已验证的非版本化Bucket流程；object_version_id虽有字段，版本化Bucket的完整VersionId持久化、历史物理删除及配额未实现/未验收。不得开启Versioning后仍宣称清理释放物理字节。无浏览器S3直传、预签名读取或匿名网站托管。

默认限制及性能边界统一见设计；备份须覆盖MySQL与所有已提交对象。降级若会丢失未发布版本的隐私来源必须拒绝或先执行经过验证的数据迁移，不可直接让旧服务公开草稿历史。
