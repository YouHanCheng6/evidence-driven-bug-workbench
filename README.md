# Evidence-Driven Bug Workbench

这是一个基于 DeerFlow 的个人 AI 缺陷调查项目：把工单、附件、可选日志和源码导航线索整理成可核验的证据，由一次全新的只读 Codex 会话完成源码调查和四段式报告，再把报告中的根因证据与修改建议确定性写回工单。

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
  → 可选日志与私有业务规则（运行时注入，不进入 Git）
  → Tabby 小候选集检索
  → 当前 checkout 逐条精确校验，最多保留 5 个源码窗口
  → 一次全新的只读 Codex 调查会话
  → 生成并校验四段式中文报告
  → 只投影第三、四节到工单备注，POST 仅一次
  → 页面优先、历史接口兜底地读回确认
  → note_written 结束，不修改源码
```

导航提示不是根因证据。进入报告的每个源码片段都必须能在选定仓库的当前 checkout 中精确复核；Codex 只能读取源码，不能编辑、安装依赖、运行修复或直接写工单。新流程也不再启动 OpenHands、独立总结模型或自动修复 Agent。历史真实 diff 仍可展示与回滚，但不能从新任务重新触发。

## 个人定制重点

| 能力 | 设计价值 | 主要实现 |
| --- | --- | --- |
| 持久状态机 | 准备、调查、报告、写回和失败状态可恢复 | `backend/app/gateway/bug_workflow_state.py` |
| 事实与范围准备 | 工单只保留一份规范副本，视觉事实优先，平台范围不靠关键词误判 | `bug_workflow.py`、`bug_investigator.py` |
| 源码导航 | Tabby 只提供小规模候选，进入上下文前按当前 checkout 精确验证 | `bug_source_retrieval.py`、`bug_source_view.py` |
| 单会话调查 | 一次只读 Codex 会话同时完成源码调查和完整四段报告 | `bug_codex_summary.py` |
| 修改建议而非执行 | 校验“主要涉及端”和本地修改范围，不自动改代码 | `bug_change_advice.py` |
| 写回幂等 | 只写第三、四节；HTML 转义、单次提交、页面优先读回 | `bug_workflow.py`、`zentao_mcp/client.py` |
| 主助手批处理 | 精确保存 Bug 选择，显式启动，按顺序逐条执行并可恢复 | `main_agent_bug_selection.py`、`main_agent_bug_batch.py`、`main_agent_workbench.py` |
| 前端证据视图 | 展示阶段、候选窗口、报告与历史结果，不在 UI 中重复编排 | `frontend/src/app/workspace/bugs/page.tsx`、`source-evidence-panel.tsx` |

完整文件定位见 [源码指南](docs/personal/SOURCE_MAP.md)。定制快照列在 [PERSONAL_EXPORT_MANIFEST.json](PERSONAL_EXPORT_MANIFEST.json)：当前收录 106 个新增或修改文件，约 5.5 万行。这里统计的是完整文件总量，包含上游原有内容，不能视为个人新增代码量。

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
