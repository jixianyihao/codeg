# AresClaw 自动看板发布

这里保留四份文档，避免方案、任务、审查与测试记录互相重复。

| 文档 | 负责内容 |
| --- | --- |
| [design.md](design.md) | 产品范围、架构、认证、权限、草稿/线上版本、查看隔离、S3与部署边界 |
| [contracts.md](contracts.md) | HTTP/CLI、结果与错误、渲染策略、MySQL/S3数据契约 |
| [progress.md](progress.md) | 唯一实施任务、审查问题、历史摘要、当前测试、环境和待办记录 |

AresClaw 仅网页模式：对话通过 Skill + Python CLI 直连独立看板服务；列表内完成管理，点击后打开公共服务独立查看页面。

公共服务采用 Python/FastAPI + MySQL + 私有S3，控制与内容两个Origin。当前设计支持 draft/published/archived，以及互不影响的线上与待发布草稿引用；更新保留看板ID/分享链接。唯一沙箱iframe位于独立内容查看页。

人机沿用W3，CLI读取既有 /root/.config/auth_token；机机使用服务自管JWT。没有MCP、会话桥或新的AresClaw Rust业务代理。

实施分支：dashboard-publishing/impl；本目录随该分支版本化。当前代码基线、未完成工作与验证只记录在progress.md，避免入口说明中的提交状态过时。

这些文档描述当前实施目标和已取得证据，不代表内网已部署/生产验收。尤其真实W3、生产负载和版本化S3不在本地测试结论内；当前S3仅验证非版本化Bucket流程。
