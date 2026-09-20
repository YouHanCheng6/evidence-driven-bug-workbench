# 源码阅读路线

建议按下面顺序阅读，而不是从页面组件反推业务状态。

| 层次 | 文件 | 作用 |
| --- | --- | --- |
| 总编排 | `backend/app/gateway/bug_workflow.py` | 串联事实准备、检索、Codex、报告校验、备注投影和读回 |
| 状态 | `bug_workflow_state.py` | 定义持久阶段、恢复点、终态和错误投影 |
| 数据契约 | `bug_workflow_models.py` | 工单、报告、HTTP 投影和历史兼容字段 |
| 输入准备 | `bug_investigator.py` | 整理规范工单、附件事实和确定性调查包，不充当第二个调查 Agent |
| 平台规则 | `bug_runtime_source_policy.py` | 平台/仓库范围与运行时源码策略 |
| 源码视图 | `bug_source_view.py` | 构建过滤后的当前 checkout 只读视图 |
| 候选检索 | `bug_source_retrieval.py` | Tabby 查询、候选平衡、精确片段复核和数量上限 |
| Codex 会话 | `bug_codex_summary.py` | 启动单个只读会话、组装提示词、校验四段报告 |
| 修改建议 | `bug_change_advice.py` | 解析并约束第四节建议，不执行修改 |
| 可选日志 | `bug_log_query_runtime.py` | 准备有边界的日志上下文 |
| 私有规则接口 | `bug_business_knowledge.py` | 运行时读取外部业务规则；公开仓库不承载实际规则 |
| 批次选择 | `main_agent_bug_selection.py` | 精确选择、保存及当前请求覆盖规则 |
| 批次执行 | `main_agent_bug_batch.py` | 按序运行、持久化和恢复 |
| 主助手适配 | `main_agent_workbench.py` | 把主助手工具调用接到唯一 Workbench 路径 |
| 内置工具 | `backend/packages/harness/deerflow/tools/builtins/main_agent_bug_batch_tools.py` | 面向主助手暴露受限的选择/批次能力 |
| 工单协议 | `backend/app/zentao_mcp/client.py` | 认证页面提交、HTML 转义与读回 |
| 路由/API | `backend/app/gateway/routers/bug_workflow.py` | HTTP 入口与状态投影 |
| 前端页面 | `frontend/src/app/workspace/bugs/page.tsx` | 工作台状态与报告呈现 |
| 证据组件 | `frontend/src/app/workspace/bugs/source-evidence-panel.tsx` | 候选源码窗口和证据状态展示 |

## 关键调用关系

```text
main_agent_workbench
  └─ main_agent_bug_selection / main_agent_bug_batch
       └─ bug_workflow
            ├─ bug_investigator
            ├─ bug_runtime_source_policy
            ├─ bug_log_query_runtime / bug_business_knowledge
            ├─ bug_source_view → bug_source_retrieval
            ├─ bug_codex_summary → bug_change_advice
            └─ zentao_mcp.client
```

源码中仍有历史兼容字段和回滚读取能力，它们服务于旧记录展示，不表示新工作流仍会启动自动修复。判断当前行为时，以 `bug_workflow.py` 的新任务路径和 `bug_workflow_state.py` 的可达状态为准。
