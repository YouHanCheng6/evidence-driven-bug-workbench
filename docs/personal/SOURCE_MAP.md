# 源码阅读路线

建议按下面顺序阅读，而不是从页面组件反推业务状态。

| 层次 | 文件 | 作用 |
| --- | --- | --- |
| 总编排 | `backend/app/gateway/bug_workflow.py` | 串联事实准备、检索、Codex、报告校验、备注读回与可选第四节修改执行 |
| 状态 | `bug_workflow_state.py` | 定义持久阶段、恢复点、终态和错误投影 |
| 数据契约 | `bug_workflow_models.py` | 工单、报告、HTTP 投影和历史兼容字段 |
| 输入准备 | `bug_investigator.py` | 整理规范工单、附件事实和确定性调查包，不充当第二个调查 Agent |
| 视觉事实 | `bug_attachment_evidence.py` | 独立、临时、无工具 Codex 截图转写；公开版已去除旧视觉模型实现 |
| 平台规则 | `bug_runtime_source_policy.py` | 平台/仓库范围与运行时源码策略 |
| 源码视图 | `bug_source_view.py` | 构建过滤后的当前 checkout 只读视图 |
| 候选检索 | `bug_source_retrieval.py` | 直接消费准备会话查询/原文锚点，双通道按文件融合，产品范围过滤与片段精确复核 |
| 文件历史 | `bug_git_history_runtime.py` | 已验证入口最多 8 条文件变更、独立提交日期/标题元数据 |
| Codex 会话 | `bug_codex_summary.py` | 启动单个只读会话、组装提示词、校验四段报告 |
| 示例 WIKI 接口 | `bug_codex_summary.py` | `ExampleWikiMcp`：假设门控、八工具白名单、运行时凭据和责任一致性校验 |
| 修改建议 | `bug_change_advice.py` | 解析并约束第四节建议，不执行修改 |
| 可选修改执行 | `bug_note_repair.py` | 本次报告第四节、正文要求清单、不重复拆代码示例、一次受限补全；默认关闭 |
| 修改快照 | `bug_rollback.py` | 前后内容快照、所有者范围内回退/恢复、并发变更与冲突保护 |
| 操作者本机交付 | `bug_git_delivery.py` | 临时 Git 索引发布中转、显式拉取为未提交修改、批准后提交原文件集当前内容并 rebase/push |
| 前置准备 | `bug_log_expert.py`、`bug_log_query_runtime.py` | 流式 SQLite 索引、分页原文查询、来源行引用恢复，以及直接 Tabby 查询/锚点；空日志仍可准备 |
| 私有规则接口 | `bug_business_knowledge.py` | 运行时读取外部业务规则；公开仓库不承载实际规则 |
| 批次选择 | `main_agent_bug_selection.py` | 精确选择、保存及当前请求覆盖规则 |
| 批次执行 | `main_agent_bug_batch.py` | 按当前请求去重、显式 IM 编号直达串行批次、持久化和恢复 |
| 主助手适配 | `main_agent_workbench.py` | 把主助手工具调用接到唯一 Workbench 路径 |
| 内置工具适配 | `backend/packages/harness/deerflow/tools/builtins/main_agent_bug_batch_tools.py`、`start_bug_workbench_tool.py` | 选择/批次/单 Bug/代码操作适配；实际模型暴露由 lead-agent 工具装配决定 |
| 飞书控制 | `backend/app/channels/feishu_operation_intent.py`、`manager.py` | 窄门控后无工具意图识别，确定性操作与本机操作者授权；不含设备配对 |
| 工单协议 | `backend/packages/zentao-mcp/src/zentao_mcp/client.py` | 认证页面提交、HTML 转义与读回 |
| 日报/看板 | `backend/packages/zentao-mcp/src/zentao_mcp/daily_report.py`、`feishu_bug_board.py` | 紧凑日报快照和可选 Bitable 投影 |
| 路由/API | `backend/app/gateway/routers/bug_workflows.py` | HTTP 入口与状态投影 |
| 前端页面 | `frontend/src/app/workspace/bugs/page.tsx` | 工作台状态、完整报告、逐项修改结果和实际文件呈现 |
| 前端状态/API | `frontend/src/core/bugs/workbench.ts`、`api.ts` | 执行中/终态与修改结果分离，回退/恢复、本机拉取及明确批准后提交的状态/请求 |
| 视频确认 | `frontend/src/components/workspace/bugs/video-clarification-card.tsx` | 唯一有效附件是视频时选择分析或跳过 |

## 关键调用关系

```text
main_agent_workbench
  └─ main_agent_bug_selection / main_agent_bug_batch
       └─ bug_workflow
            ├─ bug_investigator
            ├─ bug_runtime_source_policy
            ├─ bug_log_expert → bug_log_query_runtime（原文引用 + 查询/锚点）
            ├─ bug_business_knowledge（仅运行时接口）
            ├─ bug_source_view → bug_source_retrieval
            ├─ bug_git_history_runtime（可选，单文件一次）
            ├─ bug_codex_summary → bug_change_advice
            ├─ zentao_mcp.client（单次提交与读回）
            └─ bug_note_repair（配置启用且备注确认后）
                 ├─ bug_rollback（实际前后快照）
                 └─ bug_git_delivery（另行启用的私有中转）

Workbench / IM rollback・restore
  └─ 所有者范围内的统一 API → bug_rollback（不调用模型）

Workbench / 已授权操作者 IM fetch・submit
  └─ 统一 API → bug_git_delivery（本机拉取 / 明确批准后的正式提交）
```

新任务的调查仍是只读；可选修改执行是备注确认后的独立路径，使用本次 `analysis_report`，不读取历史备注作为修改指令。源码还保留历史兼容字段和真实 diff 的读取能力，但公开仓库不附带企业 diff 数据，也不能据此认为旧 OpenHands 或恢复引擎仍能启动。判断当前行为时，以 `bug_workflow.py` 的新任务路径、`bug_workflow_state.py` 的可达状态及 `repair_outcome` 为准：`note_written` 是终态，不等于所有修改要求完成。

本目录不提供同事本机执行器、设备配对、远端队列/结果服务、专用路由或分发压缩包；不要把这些未纳入功能误认为操作者本机交付的依赖。
