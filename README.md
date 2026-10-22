# Evidence-Driven Bug Workbench

这是一个基于 DeerFlow 的个人 AI 缺陷调查项目：把工单、附件、可选日志和源码导航线索整理成可核验的证据，由一次全新的只读 Codex 会话完成源码调查和四段式报告，再把报告中的根因证据与修改建议确定性写回工单。备注确认后，可按配置启用独立、受限的修改执行器，落实本次报告已经明确的本地修改。

这个仓库展示的是我在公开 DeerFlow 框架之上完成的工作流、提示词、状态机、证据治理、模型边界、批处理接入和前端工作台定制。它不是公司产品源码，不包含真实工单、公司源码镜像、业务知识库、日志、附件、员工资料或私有连接配置。

当前是**脱敏源码与设计展示版**：保留完整、可阅读的框架文件副本，但不承诺开箱即用。企业项目名称、人员、产品和服务已替换为虚构示例，外部系统需要使用者自行接入并验证。

## 阅读入口

- [完整流程与架构边界](docs/personal/ARCHITECTURE.md)
- [源码阅读路线](docs/personal/SOURCE_MAP.md)
- [提示词和报告约束](docs/personal/PROMPTS.md)
- [失败恢复与幂等设计](docs/personal/FAILURE_MODES.md)
- [完全虚构的端到端案例](docs/personal/SYNTHETIC_DEMO.md)
- [脱敏范围与限制](docs/personal/REDACTION_AND_LIMITATIONS.md)

## 当前流程

```text
完整工单 + 附件事实
  → 确认客户端/仓库范围
  → 可选日志索引 + 只读 Codex 日志事实选择
  → 可选私有业务规则（运行时注入，不进入 Git）
  → Tabby 原生 BM25 / 向量 / RRF 混合检索
  → 按证据来源优先级融合，产品配置只限定源码范围
  → 当前 checkout 逐条精确校验，最多保留 5 个源码窗口
  → 一次全新的只读 Codex 调查会话
  → 按材料角色核对入口、故障机制、现象与责任边界
  → 有嵌入式故障证据时，可选查询示例 WIKI 只读接口
  → 明确回归/版本问题或已知直接变更时，才允许一次文件级历史查询
  → 生成并校验四段式中文报告
  → 只投影第三、四节到工单备注，POST 仅一次
  → 页面优先、历史接口兜底地读回确认
  → 未启用修改：note_written 结束
  → 已启用修改：执行本次报告第四节的明确本地修改
     → 逐项结果 + 实际文件清单 + 前后快照 → note_written 结束
```

导航提示不是根因证据。进入报告的每个源码片段都必须能在选定仓库的当前 checkout 中精确复核。调查 Codex 只能读取源码，不能编辑、安装依赖、运行修复或直接写工单；可选修改执行器是备注确认后的独立权限边界，不重新调查，也不运行命令、测试或 Git 提交。公开示例的 `bug_analysis.repair.enabled` 默认关闭，启用前必须自行配置模型、仓库权限并验证。新流程不启动 OpenHands 或独立总结/检查模型。

2026-10-22 更新：日志专家在临时只读 Codex 回合中查询流式索引，只返回原始来源与行号；后端恢复完整脱敏记录供最终调查，不再用信号打分、截断摘录或事务合并代替原始证据，也不按固定条数丢弃新增事实。检索保留字面证据优先级；修改执行逐项区分完成、部分完成、未完成和无需修改；回退/恢复复用前后快照，不调用模型。截图仍由独立、临时、无工具的 Codex 回合提取，直接启动指定线程，公开副本不包含旧视觉模型实现。

可选 `examplewiki` 仅在已有证据形成嵌入式故障假设后使用；八个 `example_*` 工具名是虚构接口，没有实际企业 WIKI 内容或连接配置。模型名称只是运行参数，不是账号凭据。

## 个人定制重点

| 能力 | 设计价值 | 主要实现 |
| --- | --- | --- |
| 持久状态机 | 准备、调查、报告、写回和失败状态可恢复 | `backend/app/gateway/bug_workflow_state.py` |
| 事实与范围准备 | 工单只保留一份规范副本，视觉事实优先，平台范围不靠关键词误判 | `bug_workflow.py`、`bug_investigator.py` |
| 源码导航 | 字面证据优先的多通道融合，产品范围过滤，按当前 checkout 精确验证 | `bug_source_retrieval.py`、`bug_source_view.py` |
| 历史核验 | 仅针对明确回归/版本问题或已知直接变更，在已验证故障入口查询一次 | `bug_git_history_runtime.py` |
| 单会话调查 | 一次只读 Codex 会话同时完成源码调查和完整四段报告 | `bug_codex_summary.py` |
| 远端证据接口 | 故障假设门控、八个示例只读工具、嵌入式责任前后一致校验 | `bug_codex_summary.py` |
| 视觉与日志事实 | 临时无工具视觉提取；日志专家选择来源/行号，后端恢复完整脱敏记录 | `bug_attachment_evidence.py`、`bug_log_expert.py`、`bug_log_query_runtime.py` |
| 有界修改执行 | 仅执行本次报告第四节，逐项追踪结果；默认关闭，不重新调查或执行测试 | `bug_note_repair.py`、`bug_change_advice.py` |
| 可逆修改 | 实际文件前后快照、所有者范围内回退/恢复、冲突保护后续编辑 | `bug_rollback.py` |
| 写回幂等 | 只写第三、四节；HTML 转义、单次提交、页面优先读回 | `bug_workflow.py`、`zentao_mcp/client.py` |
| 主助手批处理 | 精确保存 Bug 选择，显式启动持久化后台批次，按顺序执行并支持取消/恢复 | `main_agent_bug_selection.py`、`main_agent_bug_batch.py`、`main_agent_workbench.py` |
| 日报投影 | 定时报告可选将已分类行按 Bug ID 投影到飞书多维表格，失败不影响文本报告 | `daily_report.py`、`feishu_bug_board.py` |
| 前端证据视图 | 展示阶段、候选窗口、报告与历史结果；只保留“唯一有效视频附件”的分析/跳过确认 | `frontend/src/app/workspace/bugs/page.tsx`、`video-clarification-card.tsx` |

完整文件定位见 [源码指南](docs/personal/SOURCE_MAP.md)，定制文件及校验哈希见 [PERSONAL_EXPORT_MANIFEST.json](PERSONAL_EXPORT_MANIFEST.json)。清单统计的是完整文件副本，包含上游原有内容，不能视为个人新增代码量。

## 上游与个人贡献边界

DeerFlow 提供基础 Agent Harness、Gateway、工具、沙箱、记忆、插件、聊天前端和部署框架。本项目不把这些上游能力冒称为从零开发。个人部分主要是缺陷调查、源码证据、报告、写回、批处理生命周期及其前后端接入；部分文件同时含有上游内容和个人修改。

公开基线为 `bytedance/deer-flow@88252e9b318d34e7e1867155ad2c77993320788e`。仓库保留原 [MIT LICENSE](LICENSE) 和版权声明；上游项目介绍保存在 [UPSTREAM_README.md](UPSTREAM_README.md)。

## 本地发布检查

```bash
python3.12 scripts/check_public_release.py
python3.12 -m unittest discover -s tests/public_release -v
```

项目最低要求 Python 3.12；用更旧解释器做 AST 检查会把新式 `type` 别名误报为语法错误。这些离线检查覆盖常见凭据、受限目录、导出哈希、Python 语法和虚构证据一致性，但不能证明绝无商业信息，也不等于完整运行测试。上传步骤见 [独立 GitHub 上传指南](docs/GITHUB_UPLOAD.md)：只在 `my` 内创建新的 Git 历史，不要把外层 DeerFlow 工作区一起提交。

本次整理结果见 [公开副本检查记录](PUBLIC_AUDIT.md)。
