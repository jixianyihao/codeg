# AresClaw 看板实施进度与验证记录

更新：2026-09-09。本文件是唯一任务、审查、测试与待办记录；设计见 [design.md](design.md)，接口见 [contracts.md](contracts.md)。后续直接更新本文件，不新增轮次交接/验收/审查文件。

## 1. 工作区与当前状态

- 方案目录：docs/aresclaw-dashboard，随功能分支版本化；原主工作区保留同步副本。
- 本次实现使用主工作区下的 .worktrees.local/dashboard-publishing；其他环境以实际检出目录为准。
- 分支：dashboard-publishing/impl；本轮检查起点 HEAD=f3f3e5ca，当时工作树干净。
- 2026-09-09本次摘要对比需求开始时，实际HEAD=9cf85770，工作树干净；此前生命周期、CLI与列表管理已存在于提交历史。本文不据此推断已合并、推送或部署。
- T5摘要扩展及T6链接修复在上述HEAD上完成；用户随后明确授权提交并推送至 git@github.com:jixianyihao/codeg.git 的 dashboard-publishing/impl 分支。提交SHA以分支Git历史为准，不把尚未执行的远端操作记为成功。
- T6外链修复随后完成。没有重启服务或改写已发布HTML；本地演示内容服务18081直接读取工作树静态资源，已核对其/render与/render.js响应和最终文件完全一致、Cache-Control=no-store。已打开的页面需从稳定看板入口重新进入以加载新查看器；这不表示内网生产部署完成。
- 用户已在演示环境发布首版看板。本轮没有修改该演示产物/数据库、重启演示服务或部署内网。
- 当前范围：美化列表；管理集成AresClaw侧边面板；创建/下架后的缓冲状态；线上版本与待发布草稿并存；不限制更新频率；人机与机机同一API。最新补充为list/show的SHA-256与大小、固定版本原文获取、对比后用稳定ID更新。
- 本轮先更新方案，再并行实施与复查。代码修改、本机功能验证和文档合并已完成，管理深链接末轮浏览器复验通过。生产部署与目标内网验收尚未进行。

## 2. 执行分工与状态

| 工作包 | 内容 | 当前状态 |
| --- | --- | --- |
| T1 生命周期 | 三态、双指针、首次发布时间、空草稿/保存/发布、恢复、草稿历史权限、迁移 | 已完成，完整服务套件141通过 |
| T2 可靠性/控制入口 | B1–B5、主体分页、旧管理表单退役、AresClaw固定入口 | 已实现；独立任务环境目标回归72通过 |
| T3 AresClaw | 卡片、管理面板、内容/版本/ACL、身份绑定未决写、10语言、浏览器体验 | 已完成；最终17项UI测试、此前API/路由测试和最终静态构建通过 |
| T4 CLI/交付 | create/save/publish两模式、请求ID与冻结、旧快照兼容、Skill/README、总体验证 | 已完成；CLI32测试通过、无跳过，部署与接口说明同步 |
| 文档合并 | 保留README/design/contracts/progress四文件 | 已合并；最终测试结果由收尾者继续更新本文件 |
| T5 摘要与原文对比 | list/show双版本SHA-256/大小；固定版本源码下载与摘要校验；稳定ID并发更新说明 | 已完成；服务及跨组件57项、全量CLI46项、前端API7项通过；文档与Skill同步 |
| T6 看板内链接 | 普通链接真实点击后新标签页打开；保留沙箱；兼容旧报告的复制拦截 | 已完成；真实Chromium10项、内容服务14项通过；本地演示响应已核对 |

实现主要位置：services/dashboard-service、src/components/dashboards、src/lib/dashboard-api.ts、src/lib/dashboard-types.ts、integrations/aresclaw-dashboard。公共服务没有新增Rust；不改Codeg原HTML文件预览。

## 3. 本轮审查 B1–B10

| 编号 | 触发与问题 | 处理/证据 |
| --- | --- | --- |
| B1 | S3已PUT、mark_uploaded前进程退出，恢复把reserved删掉，丢孤儿坐标 | PUT前mark_upload_started持久化；完整Publisher失联与真实S3清理测试通过 |
| B2 | 两清理worker同时结算同reservation，双扣配额 | 最终reservation FOR UPDATE；真实MySQL双worker回归先失败后通过 |
| B3 | 过期重试拿旧快照后误失败新attempt | operation锁与attempt/lease CAS；旧快照、双并发重试通过 |
| B4 | MySQL naive时间与新aware端点比较抛TypeError | 统一UTC；单端点修改先失败后通过 |
| B5 | 真实FastAPI超限请求连发413和400 | 受控HTTP异常、统一一次413；JSON/multipart实际框架回归通过 |
| B6 | 版本更新省略description清空原说明 | 区分省略/显式空串；生命周期实现和回归覆盖，兼容指纹另复核 |
| B7 | 同级多授权到期取最早，错误显示提前失效 | 改取最后有效来源；生命周期定向覆盖 |
| B8 | shared混入未授权卡片，主体目录永无下一页 | shared有效授权过滤；跨类型同名keyset与签名游标回归通过 |
| B9 | 旧管理页202报成功、未决写未绑定身份、刷新自动打开查看 | 公共表单退役；面板、身份绑定未决写与专项交互回归完成；浏览器保存不会自动打开查看 |
| B10 | CLI冻结后才规范request_id、快照子目录链接逃逸 | 提前UUID校验与逐层reparse检查；CLI定向测试已覆盖 |

T2同时发现健康探针 /api/v1/health/deep 使用未导入text，已以实际端点先复现NameError，再修复并纳入回归。

## 4. 收尾独立复查

| 项目 | 发现与要求 | 状态 |
| --- | --- | --- |
| CLI旧快照 | 旧update未传description被冻结为空串，新版省略为None导致同键重试冲突；只对识别的legacy快照保留原语义 | 已修复；新增兼容与省略/显式清空冲突测试通过 |
| 服务旧指纹 | 新“省略保持”不能破坏旧operation的空description指纹，也不能混淆新请求 | 新内部action=publish_v2，旧action=publish保留旧指纹；三类重试回归通过 |
| operation草稿隐私 | editor降viewer后不能通过自己的旧operation读取未发布版本或相关draft指针 | 已补版本资格与结果脱敏；包含在141项服务回归中 |
| 降级隐私 | 已上线看板含未发布历史时，删除published_at标记后运行旧服务会公开草稿 | 任何草稿引用/状态或未发布历史存在时，在所有DDL前拒绝降级；真实MySQL测试确认schema与指针不变 |
| 页签切换未决写 | 离开看板页导致页面卸载，丢失冻结请求与重试入口 | 已改为有界内存状态；卸载/重挂载回归校验同一请求对象，独立复审通过 |
| 可重试失败 | 查询返回failed且retryable=true时过早清掉原请求 | 已保留原UUID/字节并提供重试；查询失败再原请求重试回归通过 |
| 操作反馈与授权名称 | 新建/切换看板出现上次成功提示；授权列表只有UUID | 新操作只清完成反馈、不清未决写；授权显示真实名称并保留ID。对应UI/API回归通过 |
| 管理深链接 | 冷启动的会话恢复把view=dashboards覆盖为对话页 | 已复现并修复；只忽略显式看板入口的首次null→tab后台激活；后续/显式本地切换与远程同步回归通过，最终构建后浏览器冷加载直接打开指定管理面板、无控制台错误 |
| S3 Versioning | 现有写读清理未完成物理VersionId全流程 | 明确不支持/未验收版本化Bucket；本轮测试为非版本化S3 |
| 主体并发门控 | max_principal_uploads=2字段未落实；现有仅每控制进程8上传槽 | 保留限制说明，不能宣称主体限流已生效 |
| 长期更新配额 | 每看板50版本；草稿也占用，无自动删除已提交历史 | 上线前确定实际保留/扩容要求，不自动删版本 |
| T5版本标识兼容 | 新源码响应头按字符串比较，导致服务接受的大写UUID被CLI误判完整性失败 | 根代理在真实服务人机/机机用例中复现；UUID按规范身份比较、非UUID保持精确比较，大小写/紧凑/花括号形式及不同UUID拒绝回归通过 |
| T6链接与来源 | 严格沙箱阻止新窗口，报告document监听又拦截链接；仅靠window消息/近期激活无法证明来自链接 | 提前注入真实锚点捕获，通过闭包私有端口打开；浏览器验证绕过旧复制面板，合成点击和无关按钮的伪造URL/READY均无效，不增加沙箱权限 |
| T6锚点与非法协议 | srcdoc的#链接默认使用父页基址，CSP阻止；不支持协议不能只return | 浏览器复现锚点不定位后修复iframe自身hash；保留报告defaultPrevented。不支持协议显式取消，使用document捕获哨兵验证，未将推测的javascript执行当作已发生漏洞 |

正向迁移已检查保留旧看板状态、线上指针、版本原文和S3引用，并回填旧版本首次发布时间。独立代码复查未发现额外的历史/source/capability草稿读取绕过；这不替代目标内网验收。

## 5. 本轮已获得的验证证据

以下结果来自对应执行者的实际运行报告，套件有交集，不能相加作为总测试数。

| 验证 | 结果 | 归属/范围 |
| --- | --- | --- |
| CLI完整测试 | 32通过、无跳过，30.18秒 | 根代理实际运行；含旧快照、草稿、指定版本发布、规范UUID、目录联接与源文件删除后重试 |
| 服务完整测试 | 141通过、4 subtests通过、8依赖弃用警告，28.54秒 | fresh final2：iteration2_final2_99030e6a；真实MySQL与非版本化MinIO |
| T2最终目标套件 | 72通过、4 subtests通过、4依赖弃用警告 | 本轮可靠性代理；专用MySQL/S3，含request guards、settlement、recovery、access、publishing guards、content及新增测试 |
| T2首批重要红例 | 4项实际失败后修复通过 | 缺PUT前标记、旧快照失败新attempt、双清理双扣、目录只取2/6 |
| T2纯框架/时间/入口 | 先失败后通过 | 真实FastAPI一次413、UTC端点、固定管理入口，无演示服务依赖 |
| 生命周期扩展回归 | 87通过；最后shared筛选补修后35通过 | 已由后续141项完整套件覆盖，不与总数相加 |
| Ruff致命规则 | 通过 | T2变更文件，E9/F63/F7/F82；完整严格Ruff仍有既有框架/样式告警 |
| 前端完整测试 | 353文件、4848测试通过，199.08秒 | 根代理实际运行；包含当时全部测试，后续两项未决请求修复由最终定向套件补验 |
| 前端最终定向测试与lint | 根代理最终4文件30测试通过；定向ESLint无错误/警告 | 17项UI、7项API、3项路由、3项启动同步；包含页签切换、可重试失败、旧提示、名称/稳定ID和深链接，10语言文案完整 |
| 静态构建 | 最终Webpack静态导出通过，含类型检查与32静态页面 | 默认Turbopack受本机已有目录联接越界影响；仅验收副本使用Next支持的--webpack，不改变产品构建配置 |
| 授权名称补修 | 24项ACL/并发测试通过，4条已知弃用警告 | 人类/群组/机机名称、失效引用ID回退、viewer禁止读取；与服务总套件有交集不相加 |
| 功能发现与Compose | 草稿capabilities定向测试1通过；YAML解析及3处JWT挂载检查通过 | 服务声明create_draft/save_draft/publish_draft；未启动Docker，未读取真实.env |
| 实际浏览器操作 | 已验证列表、管理、空草稿、HTML暂存、草稿预览、发布v2、名称与有效期授权、下架/恢复、Escape关闭面板、冷启动管理深链接 | 正常Web静态导出与真实服务；独立loopback资源，未修改演示数据；AresClaw iframe数为0，独立内容页仅1个allow-scripts沙箱 |
| 摘要元数据与源码头 | 55项定向服务测试通过，8条已知弃用警告 | 新6例先因缺字段/响应头失败，再通过；包括真实MySQL/S3、角色脱敏、空草稿/下架、原始换行、不访问S3的列表、损坏对象与无权限不返回摘要头；fresh versionmetadata lane |
| 摘要字段网页兼容 | 7项现有API测试通过；类型文件ESLint通过 | 只补可选类型字段，兼容旧服务；无网页交互变更。Vitest沙箱内esbuild读取配置失败，使用现有依赖在沙箱外运行成功，未改变构建配置 |
| T5最终服务及跨组件回归 | 57通过、8条已知弃用警告，12.27秒 | 根代理实际运行，fresh hash_final：iteration2_hash_final_06e349b7；包含前述55项以及人机/机机CLI生产解析/编码与真实ASGI/MySQL/S3链路，socket层使用TestClient；快照后发生更新仍下载原版、默认current、UUID兼容、旧revision拒绝与历史保留均通过 |
| T5最终全量CLI回归 | 46通过、无跳过，44.707秒 | CLI代理实际运行并报告；含新增14项源码测试，真实临时HTTP服务与CLI子进程，初始缺参数/校验及UUID问题先失败后通过；没有生成跟踪中的pycache改动 |
| T5静态与独立审查 | 定向Ruff致命规则、ESLint、git diff --check通过 | 根代理检查代码与跨组件行为，独立代理核查权限、指针快照和校验；未进行额外网页构建/浏览器操作，因本次网页只增加可选类型字段 |
| T6真实浏览器 | Chromium定向10通过、0失败，4.14秒 | 浏览器代理执行node --test --test-name-pattern sandbox services/dashboard-service/web/tests/browser.test.mjs；包含左键/中键/回车、旧报告拦截、opener/referrer、伪造消息、非法地址、备用入口、锚点和原沙箱隔离。目的地为任务本地HTTP服务；正常目标站自身Cookie/SSO不在“无referrer/opener”承诺内。未运行遗留独立管理表单用例，不宣称全套浏览器通过 |
| T6服务与静态验收 | 内容API14通过、4条已知弃用警告；JS语法/ESLint、3文件Prettier、diff检查通过 | 根代理实际执行；fresh link_content：iteration2_link_content_87fda781，专用MySQL23307/S329001；复核本地演示/render与/render.js均HTTP200且匹配最终工作树文件。没有改变S3原文/摘要/版本记录 |
| 提交前完整服务回归 | 150通过、4 subtests通过、8条已知弃用警告，29.95秒 | fresh prepush：iteration2_prepush_1378001e；服务代理实际执行完整tests目录，任务专用MySQL23307/S329001 |
| 提交前CLI/API回归 | CLI46通过、45.501秒；API7通过、16.44秒 | 验证代理实际执行；关闭Python字节码生成，没有pycache变更；API保留Vite CJS已知弃用警告 |
| 提交前浏览器回归 | 备用入口2通过；最终沙箱10通过、0失败，4.31秒 | 首次复跑9/10暴露旧弹窗异步关闭导致目标集合变小的测试竞态；修正为检查没有新增页面目标，并在夹具就绪后记录真实点击基线，仍拒绝任何意外新窗口；只修改测试，未放宽产品校验 |

T6浏览器测试初始真实点击先失败；实施后修复锚点及明确取消问题。测试过程中另定位到CDP页面焦点/绘制时机和同文档fragment导航造成的输入丢失，测试改为每用例经about:blank加载、等待子页就绪和双动画帧、真实操作前bringToFront，无重复点击重试；这些是测试夹具修正，没有因此放宽产品校验。

已知测试输出警告来自Starlette/httpx、AnyIO和Alembic配置弃用提示；不是“零警告通过”。本轮功能套件不构成性能压测、真实W3或版本化S3验收。

## 6. 测试环境与执行方式

工作树自带Python：services/dashboard-service/.venv/Scripts/python.exe；前端使用该工作树node_modules与corepack pnpm。根目录没有可替代服务venv的 .venv。

本轮独立测试资源由根代理创建：MySQL 23307、S3 29001；隔离lane包括 reliability、lifecycle、final、final2。运行脚本位于：
原主工作区的 .aresclaw-build/dashboard-iteration2-tests/run.py（本地任务辅助脚本，不随仓库交付）。

从实现工作树执行目标服务测试示例：

```powershell
& '.\services\dashboard-service\.venv\Scripts\python.exe' '../../.aresclaw-build/dashboard-iteration2-tests/run.py' reliability tests/test_iteration2_reliability_mysql.py -q
```

每次运行均生成新的随机后缀数据库与Bucket，避免初始化迁移碰到上一轮草稿数据。脚本由任务环境管理，不是生产部署入口；不要复制凭据到文档或命令输出。

从实现工作树执行CLI/前端检查：

```powershell
& '.\services\dashboard-service\.venv\Scripts\python.exe' -m unittest discover -s integrations/aresclaw-dashboard/tests -v
corepack pnpm exec vitest run src/lib/dashboard-api.test.ts src/components/dashboards/dashboard-page.test.tsx
corepack pnpm test
corepack pnpm eslint .
corepack pnpm build
```

服务原pytest fixture会迁移/清数据，必须检查当前fixture并使用本轮专用空库/Bucket；不能对演示库运行downgrade或DELETE。降级隐私保护若新增，测试初始化也要适配专用资源，不能为通过测试取消生产保护。

浏览器验收使用独立入口27100、Codeg27300、控制28180、内容28181，以及专用UI数据库/Bucket。代理转发HTTP与WebSocket；UI副本仅复制源码/配置，不复制真实.env。启动记录在任务目录ui-processes.json，UI数据在ui-state.json；不能按这些记录中的旧PID盲目杀进程，应同时核验当前路径、父子关系与端口。

浏览器局限：当前in-app viewport覆盖请求未改变实际1280×720宽高，因此不宣称390px窄屏验收通过。日期控件通过真实方向键输入确认后，成功保存并显示Asia/Shanghai有效期；文件选择器一次等待异常偏长，但实际选中文件并成功保存为草稿。两者不冒充产品性能测试结果。

历史演示端口仅作避免误操作的线索：AresClaw入口7000、服务7100、控制18080、内容18081、开发认证18090、旧MySQL13306、旧S3 19000。不得按旧PID杀进程、全局nginx stop、盲删构建锁或重建这些库。

保留用户 .aresclaw-build、.codegraph、output、聊天产物和现有worktree；不读取真实Token/密钥。新构建需要配置实际固定控制Origin，不能把本地测试地址打包为内网生产地址。

## 7. 上轮历史摘要（非本轮验收）

早期基点aad6ad3e及ba027e69阶段的交接已被后续实现替代；本轮起点为f3f3e5ca。原R1–R15曾提出以下问题，后续已有修复提交，本轮发现其中仍有B系列边界遗漏：

| 原编号 | 历史主题 |
| --- | --- |
| R1–R3 | 清理与提交竞态、未知PUT配额、旧attempt覆盖新结果 |
| R4–R5 | 最终事务实时权限与同revision并发写 |
| R6–R8 | 实际请求体限额、授权时间更新、幂等重放重新授权 |
| R9–R11 | 机器列表read scope、过期operation恢复、配额满时成功重放 |
| R12 | 控制页内联CSS被CSP阻断，查看区尺寸异常 |
| R13–R15 | CLI群成员重放、failed退出码、源文件删除后冻结重试 |

旧实现方曾报告服务77项、CLI15项、浏览器9项及前端构建/测试；不同旧报告前端数字不一致，且非本轮独立重跑。仅保留历史背景，不继续散布旧“全部完成/全部未修复”的结论。

## 8. 最终收尾清单

- [x] 确认最新CLI兼容、operation草稿隐私与安全降级修复及其测试。
- [x] 汇总当前完整服务、CLI、前端测试/lint/build的通过、失败、跳过与警告。
- [x] 浏览器核查列表布局、侧边管理、键盘关闭与日期输入、无自动打开；当前身份与未决请求边界由专项测试补验。
- [ ] 390px等窄屏实机验收（本地viewport覆盖未生效，明确不计通过）。
- [x] 专用HTTP/MySQL/S3验收：线上v1→草稿v2→viewer仍读v1且读不到草稿历史→发布v2→回滚→下架→恢复仍为draft→显式再发布。
- [x] 同步部署说明、Skill、配置示例；核查服务启用功能探测与UI字段一致。
- [x] 最终代码复核与文档链接检查；只保留四份主方案文档。
- [x] 记录真实W3/企业CA/SSO Cookie/生产S3条件写/容量与备份恢复的内网待验收项。

最终交付须报告真实工作树状态和未完成项。用户已明确授权将本轮代码、测试和四份文档提交并推送至上述仓库的同名功能分支；合并主分支、迁移演示数据库和内网生产部署不在本次操作中。
