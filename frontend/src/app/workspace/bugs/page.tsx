"use client";

import {
  ArrowRight,
  CheckCircle2,
  ClipboardCheck,
  FileSearch,
  GitPullRequest,
  TriangleAlert,
} from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ProductClarificationCard,
  type ProductClarificationSubmission,
} from "@/components/workspace/bugs/product-clarification-card";
import { SourceEvidencePanel } from "@/components/workspace/bugs/source-evidence-panel";
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";
import {
  acceptBugWorkflow,
  createBugWorkflow,
  fetchBugWorkflow,
  retryBugWorkflowNote,
  rollbackBugWorkflow,
  submitBugWorkflowClarification,
  type BugWorkflow,
} from "@/core/bugs/api";
import {
  getBugEvidenceAssetStatusLabel,
  getBugWorkflowStep,
  getBugWorkflowStatusLabel,
  getOpenHandsInvestigationStatusLabel,
  normalizeBugId,
  resolveBugWorkflowId,
} from "@/core/bugs/workbench";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

const workflowSteps = [
  { label: "读取禅道", icon: FileSearch },
  { label: "准备辅助线索", icon: GitPullRequest },
  { label: "Bug Workbench 调查", icon: FileSearch },
  { label: "总结与修改意见", icon: ClipboardCheck },
  { label: "写入禅道备注", icon: ClipboardCheck },
  { label: "交付完成", icon: CheckCircle2 },
];

const LAST_WORKFLOW_STORAGE_KEY = "deerflow:bug-workbench:last-workflow";

function formatTokenCount(value: number | undefined): string {
  return Math.max(0, value ?? 0).toLocaleString("en-US");
}

function investigationModeLabel(value: unknown): string {
  const labels: Record<string, string> = {
    android: "Android（共享 RN + Android Native 候选）",
    ios: "iOS（共享 RN + iOS Native 候选）",
    android_ios_shared: "Android 与 iOS（共享 RN）",
    classic_shared_unknown: "客户端未知（共享 RN 优先）",
    harmony: "Harmony 仓库",
  };
  return labels[typeof value === "string" ? value : ""] ?? "未确认";
}

export default function BugWorkbenchPage() {
  const { t } = useI18n();
  const searchParams = useSearchParams();
  const routeWorkflowId = searchParams.get("workflow");
  const [rawBugId, setRawBugId] = useState("");
  const [submittedBugId, setSubmittedBugId] = useState<string | null>(null);
  const [workflow, setWorkflow] = useState<BugWorkflow | null>(null);
  const [starting, setStarting] = useState(false);
  const [submittingClarification, setSubmittingClarification] = useState(false);
  const [submittingAcceptance, setSubmittingAcceptance] = useState(false);
  const [retryingNote, setRetryingNote] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const normalizedBugId = normalizeBugId(rawBugId);
  const isRunning =
    workflow?.status === "routing" ||
    workflow?.status === "analyzing" ||
    workflow?.status === "writing_note" ||
    workflow?.status === "repairing";
  const shouldPoll =
    workflow !== null &&
    ![
      "analysis_incomplete",
      "note_written",
      "accepted",
      "rolled_back",
      "skipped",
      "cancelled",
      "failed",
    ].includes(workflow.status);
  const activeStep = getBugWorkflowStep(
    workflow?.status,
    submittedBugId !== null,
    workflow?.repair_attempted === true,
  );
  const showingAnalysisDraft = workflow?.handoff_state === "analysis_draft";
  const displayedHandoff = showingAnalysisDraft
    ? workflow?.analysis_summary
    : (workflow?.analysis_report ?? workflow?.handoff);
  useEffect(() => {
    document.title = `${t.sidebar.bugWorkbench} - ${t.pages.appName}`;
  }, [t.pages.appName, t.sidebar.bugWorkbench]);

  useEffect(() => {
    const workflowId = resolveBugWorkflowId(
      routeWorkflowId,
      window.localStorage.getItem(LAST_WORKFLOW_STORAGE_KEY),
    );
    if (!workflowId) return;
    let cancelled = false;
    setError(null);
    setWorkflow(null);
    setSubmittedBugId(null);
    window.localStorage.setItem(LAST_WORKFLOW_STORAGE_KEY, workflowId);
    void fetchBugWorkflow(workflowId)
      .then((restoredWorkflow) => {
        if (cancelled) return;
        setWorkflow(restoredWorkflow);
        setSubmittedBugId(String(restoredWorkflow.bug_id));
      })
      .catch(() => {
        if (cancelled) return;
        if (
          window.localStorage.getItem(LAST_WORKFLOW_STORAGE_KEY) === workflowId
        ) {
          window.localStorage.removeItem(LAST_WORKFLOW_STORAGE_KEY);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [routeWorkflowId]);

  useEffect(() => {
    if (!workflow || !shouldPoll) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void fetchBugWorkflow(workflow.id)
        .then((updatedWorkflow) => {
          if (!cancelled) setWorkflow(updatedWorkflow);
        })
        .catch((pollError) => {
          if (cancelled) return;
          setError(
            pollError instanceof Error
              ? pollError.message
              : "无法读取流程状态。",
          );
        });
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [shouldPoll, workflow]);

  async function startWorkflow() {
    if (!normalizedBugId) return;
    setStarting(true);
    setError(null);
    setWorkflow(null);
    setSubmittedBugId(normalizedBugId);
    try {
      const createdWorkflow = await createBugWorkflow(normalizedBugId);
      window.localStorage.setItem(
        LAST_WORKFLOW_STORAGE_KEY,
        createdWorkflow.id,
      );
      setWorkflow(createdWorkflow);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法启动自动分析与备注流程。",
      );
    } finally {
      setStarting(false);
    }
  }

  async function resumeWithClarification(
    submission: ProductClarificationSubmission,
  ) {
    if (!workflow) return;
    setSubmittingClarification(true);
    setError(null);
    try {
      const resumedWorkflow = await submitBugWorkflowClarification(
        workflow.id,
        submission,
      );
      setWorkflow(resumedWorkflow);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法提交修改目标确认。",
      );
    } finally {
      setSubmittingClarification(false);
    }
  }

  async function finishRepair(action: "accept" | "rollback") {
    if (!workflow) return;
    setSubmittingAcceptance(true);
    setError(null);
    try {
      const updatedWorkflow =
        action === "accept"
          ? await acceptBugWorkflow(workflow.id)
          : await rollbackBugWorkflow(workflow.id);
      setWorkflow(updatedWorkflow);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法完成本次修复验收。",
      );
    } finally {
      setSubmittingAcceptance(false);
    }
  }

  async function retryNote() {
    if (!workflow) return;
    setRetryingNote(true);
    setError(null);
    try {
      setWorkflow(await retryBugWorkflowNote(workflow.id));
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法重新生成或写入禅道备注。",
      );
    } finally {
      setRetryingNote(false);
    }
  }

  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:p-8">
          <section className="relative overflow-hidden rounded-2xl border bg-[linear-gradient(115deg,color-mix(in_oklab,var(--primary)_11%,transparent),transparent_48%)] p-6 sm:p-8">
            <div className="bg-primary/10 absolute -top-16 -right-10 size-44 rounded-full blur-3xl" />
            <div className="relative max-w-3xl">
              <Badge
                variant="outline"
                className="border-primary/30 bg-primary/5 text-primary"
              >
                Codex 只读调查并保存备注
              </Badge>
              <h1 className="mt-4 text-3xl font-semibold tracking-tight sm:text-4xl">
                Bug 工作台
              </h1>
              <p className="text-muted-foreground mt-3">
                DeerFlow 会读取工单与附件、确认复现端和仓库，并准备有界的日志与业务知识；Tabby
                用精确锚点检索源码，再通过公司 embedding 对有界候选重排；只有在当前 checkout 中逐字核对通过的片段才会交给 Codex。随后一个只读 Codex
                会话在选定仓库中调查源码并直接生成完整四段中文报告。系统只把第三、第四部分写入禅道并回读确认；不会自动修改代码。
              </p>
            </div>
          </section>

          <section className="bg-card grid gap-4 rounded-2xl border p-5 shadow-sm lg:grid-cols-[1fr_auto] lg:items-end">
            <div>
              <label htmlFor="zentao-bug-id" className="text-sm font-medium">
                禅道 Bug ID
              </label>
              <p className="text-muted-foreground mt-1 text-sm">
                支持输入 <code>83697</code>、<code>#83697</code> 或{" "}
                <code>Bug #83697</code>。
              </p>
              <Input
                id="zentao-bug-id"
                className="mt-3 max-w-xl"
                value={rawBugId}
                onChange={(event) => setRawBugId(event.target.value)}
                placeholder="例如：83697"
                aria-invalid={rawBugId.length > 0 && !normalizedBugId}
              />
              {rawBugId.length > 0 && !normalizedBugId ? (
                <p className="text-destructive mt-2 flex items-center gap-1 text-sm">
                  <TriangleAlert className="size-4" /> 请输入纯数字 Bug ID。
                </p>
              ) : null}
            </div>
            <Button
              onClick={startWorkflow}
              disabled={!normalizedBugId || starting || isRunning}
            >
              {starting ? "正在启动" : "开始分析"} <ArrowRight />
            </Button>
          </section>

          <section
            className="grid gap-3 md:grid-cols-3 xl:grid-cols-6"
            aria-label="Bug 只读调查、四段报告与备注工作流"
          >
            {workflowSteps.map((item, index) => {
              const Icon = item.icon;
              const active = index <= activeStep;
              return (
                <div
                  key={item.label}
                  className={cn(
                    "flex items-center gap-3 rounded-xl border p-4 text-sm transition-colors",
                    active
                      ? "border-primary/35 bg-primary/5"
                      : "bg-muted/25 text-muted-foreground",
                  )}
                >
                  <Icon className={cn("size-4", active && "text-primary")} />
                  <span className="font-medium">{item.label}</span>
                  {active ? (
                    <CheckCircle2 className="text-primary ml-auto size-4" />
                  ) : null}
                </div>
              );
            })}
          </section>

          {submittedBugId ? (
            <>
              <section className="grid gap-5 lg:grid-cols-[1.35fr_0.85fr]">
                <div className="bg-card rounded-2xl border p-5 shadow-sm">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <p className="text-muted-foreground text-sm">当前任务</p>
                      <h2 className="mt-1 text-xl font-semibold">
                        ZenTao Bug #{submittedBugId}
                      </h2>
                    </div>
                    <Badge variant="secondary">
                      {workflow?.status === "analyzing" &&
                      workflow.analysis_engine === "codex"
                        ? "Codex 调查中"
                        : workflow?.status === "analyzing" &&
                            workflow.analysis_engine === "openhands"
                          ? "Bug Workbench 调查中"
                        : getBugWorkflowStatusLabel(
                            workflow?.status,
                            workflow?.clarification_type,
                            workflow?.repair_attempted === true,
                            workflow?.failure_kind,
                          )}
                    </Badge>
                  </div>
                  {workflow?.revision ? (
                    <p className="text-muted-foreground mt-2 text-xs">
                      状态版本 revision {workflow.revision}
                      {workflow.last_event?.actor_source
                        ? ` · 最近更新来自 ${
                            workflow.last_event.actor_source === "feishu"
                              ? "飞书"
                              : workflow.last_event.actor_source === "workbench"
                                ? "工作台"
                                : workflow.last_event.actor_source ===
                                    "main_agent"
                                  ? "主 Agent"
                                  : "系统"
                          }`
                        : ""}
                      {workflow.last_event?.summary
                        ? ` · ${workflow.last_event.summary}`
                        : ""}
                    </p>
                  ) : null}

                  <div className="bg-muted/20 mt-5 rounded-xl border border-dashed p-4">
                    <p className="text-sm font-medium">真实自动执行链路</p>
                    <p className="text-muted-foreground mt-2 text-sm leading-6">
                      {workflow?.analysis_engine === "codex"
                        ? "DeerFlow 整理完整工单、附件事实、复现端、关联日志及少量可忽略源码入口，选定只读仓库视图。一个 Codex 会话自行核实或推翻线索、调查源码与运行边界，并在同一轮的最终回复中提交四段中文报告。DeerFlow 只负责保存、禅道备注写入与回读；不自动改代码。"
                        : workflow?.analysis_engine === "openhands"
                        ? "禅道 module 先确定仓库家族，设备/系统事实再确认具体复现端；缺少客户端事实时会显式降级为 module 范围并继续调查，不会伪造 Android/iOS 复现。DeerFlow 随后准备工单、附件、日志、业务别名和经过当前源码校验的 Tabby 候选；一个新的只读 Codex 会话在选定仓库中完成源码调查并生成四段报告。候选仅用于导航，不作为根因证据；Codex 可在已读到精确符号后使用一次有界 Tree-sitter 查询。"
                        : "这是旧版本保存的工作流记录；页面只展示其已持久化结论，不再启动旧专家。"}
                      {workflow?.analysis_engine === "openhands"
                        ? " 调查后由独立 Codex 会话总结；建议未实施、未验证，不启动修复专家。"
                        : ""}
                    </p>
                  </div>

                  {workflow?.platform_resolution ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="font-medium">复现端与仓库范围</p>
                        <Badge variant="outline">
                          {workflow.platform_resolution.client_scope_status ===
                          "module_fallback"
                            ? "module 降级"
                            : `${workflow.platform_model_call_count ?? 0} 次无工具判断`}
                        </Badge>
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        module
                        只确定仓库家族；设备、系统与正文确定复现端。缺失时保留“客户端未知”并从 module 对应仓库继续，RN/原生归属仍由主调查读取源码判断。
                      </p>
                      <dl className="mt-3 grid gap-2 sm:grid-cols-2">
                        <div>
                          <dt className="text-muted-foreground inline">
                            复现端：
                          </dt>
                          <dd className="inline font-medium">
                            {workflow.platform_resolution.reported_clients
                              ?.length
                              ? workflow.platform_resolution.reported_clients.join(
                                  "、",
                                )
                              : workflow.platform_resolution
                                    .client_scope_status === "module_fallback"
                                ? "工单未提供（按 module 降级）"
                                : "未确认"}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground inline">
                            主仓库：
                          </dt>
                          <dd className="inline font-medium">
                            {workflow.platform_resolution.primary_repository ??
                              "未确认"}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground inline">
                            调查模式：
                          </dt>
                          <dd className="inline">
                            {investigationModeLabel(
                              workflow.platform_resolution.investigation_mode,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground inline">
                            判断模型：
                          </dt>
                          <dd className="inline">
                            {workflow.platform_model ?? "未上报"}
                          </dd>
                        </div>
                      </dl>
                      {workflow.platform_resolution.reason ? (
                        <p className="mt-3 text-xs leading-5">
                          {workflow.platform_resolution.reason}
                        </p>
                      ) : null}
                      {workflow.platform_resolution.evidence?.length ? (
                        <div className="bg-muted/30 mt-3 rounded-lg px-3 py-2 text-xs leading-5">
                          {workflow.platform_resolution.evidence.map(
                            (item, index) => (
                              <p key={`${item.source ?? "fact"}-${index}`}>
                                {item.source ?? "工单"}：{item.quote ?? ""}
                              </p>
                            ),
                          )}
                        </div>
                      ) : null}
                    </div>
                  ) : null}

                  {workflow?.source_retrieval ? (
                    <SourceEvidencePanel retrieval={workflow.source_retrieval} />
                  ) : null}

                  {workflow?.analysis_engine === "codex" ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="font-medium">Codex 只读调查与报告</p>
                        <Badge variant="outline">
                          {workflow.status === "failed" &&
                          workflow.failure_kind === "analysis_execution_failed"
                            ? "源码调查失败"
                            : workflow.status === "failed"
                              ? "调查流程失败"
                            : workflow.external_execution_status === "finished"
                            ? "调查与报告已完成"
                            : workflow.codex_progress?.phase === "report_completed"
                              ? "报告已生成"
                              : "源码调查中"}
                        </Badge>
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        {workflow.summary_model ?? "Codex 模型待启动"}
                        {workflow.codex_thread_id
                          ? ` · 会话 ${workflow.codex_thread_id}`
                          : ""}
                        {typeof workflow.codex_progress?.elapsed_seconds ===
                        "number"
                          ? ` · 已用 ${workflow.codex_progress.elapsed_seconds}s`
                          : ""}
                        {typeof workflow.codex_progress?.completed_items ===
                        "number"
                          ? ` · 已完成 ${workflow.codex_progress.completed_items} 项会话事件`
                          : ""}
                      </p>
                    </div>
                  ) : workflow?.analysis_engine === "openhands" ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="font-medium">Bug Workbench 主调查 Agent</p>
                        <Badge variant="outline">
                          {getOpenHandsInvestigationStatusLabel(
                            workflow.external_execution_status,
                          )}
                        </Badge>
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        当前模型：
                        {workflow.external_models?.length
                          ? workflow.external_models.join("、")
                          : "等待 Bug Workbench 上报"}
                        {workflow.external_conversation_id
                          ? ` · 会话 ${workflow.external_conversation_id}`
                          : ""}
                      </p>
                      {workflow.external_request_diagnostics ? (
                        <p className="text-muted-foreground mt-1 text-xs leading-5">
                          请求诊断：
                          {workflow.external_request_diagnostics.route ?? "未知路由"}
                          {typeof workflow.external_request_diagnostics.attempts ===
                          "number"
                            ? ` · 尝试 ${workflow.external_request_diagnostics.attempts}`
                            : ""}
                          {typeof workflow.external_request_diagnostics.timeouts ===
                          "number"
                            ? ` · 超时 ${workflow.external_request_diagnostics.timeouts}`
                            : ""}
                          {typeof workflow.external_request_diagnostics.last_elapsed_ms ===
                          "number"
                            ? ` · 最近耗时 ${Math.round(workflow.external_request_diagnostics.last_elapsed_ms / 100) / 10}s`
                            : ""}
                        </p>
                      ) : null}
                      {workflow.summary_model ? (
                        <p className="text-muted-foreground mt-1 text-xs leading-5">
                          最终四段式总结：{workflow.summary_model}
                          {workflow.summary_model_call_count
                            ? workflow.summary_model.startsWith("codex/")
                              ? ` · ${workflow.summary_model_call_count} 次总结会话`
                              : ` · ${workflow.summary_model_call_count} 次调用`
                            : " · 等待总结"}
                        </p>
                      ) : null}
                      {workflow.codex_thread_id ? (
                        <p className="text-muted-foreground mt-1 text-xs leading-5">
                          Codex 总结会话：{workflow.codex_thread_id}
                        </p>
                      ) : null}
                      {Object.keys(workflow.external_conversations ?? {})
                        .length ? (
                        <p className="text-muted-foreground mt-1 text-xs leading-5">
                          调查会话：
                          {Object.entries(workflow.external_conversations ?? {})
                            .map(
                              ([targetId, conversationId]) =>
                                `${targetId} ${conversationId}`,
                            )
                            .join(" · ")}
                        </p>
                      ) : null}
                      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            总 token
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_token_usage?.total_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输入
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_token_usage?.prompt_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输出
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_token_usage?.completion_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            缓存读取
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_token_usage?.cache_read_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            推理 token
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_token_usage?.reasoning_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            模型调用
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_model_call_count,
                            )}{" "}
                            次
                          </dd>
                        </div>
                        {workflow.external_accumulated_cost !== null &&
                        workflow.external_accumulated_cost !== undefined ? (
                          <div>
                            <dt className="text-muted-foreground text-xs">
                              Provider 上报费用
                            </dt>
                            <dd className="mt-1 font-medium tabular-nums">
                              {workflow.external_accumulated_cost.toFixed(6)}
                            </dd>
                          </div>
                        ) : null}
                      </dl>
                      <div className="bg-muted/30 mt-3 rounded-lg px-3 py-2 text-xs leading-5">
                        <span className="text-muted-foreground">
                          当前缺失证据：
                        </span>
                        {workflow.investigation_missing_evidence?.length
                          ? workflow.investigation_missing_evidence.join("、")
                          : "无"}
                      </div>
                    </div>
                  ) : null}

                  {(workflow?.review_model_call_count ?? 0) > 0 ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="font-medium">历史任务 Reviewer 记录</p>
                        <Badge variant="outline">
                          {workflow?.review_model_call_count} 次调用
                        </Badge>
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        仅用于兼容旧任务；模型：
                        {workflow?.review_model ?? "未上报"}
                      </p>
                      <dl className="mt-3 grid grid-cols-3 gap-3">
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            总 token
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow?.review_token_usage?.total_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输入
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow?.review_token_usage?.input_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输出
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow?.review_token_usage?.output_tokens,
                            )}
                          </dd>
                        </div>
                      </dl>
                    </div>
                  ) : null}

                  {workflow?.repair_engine === "patch_executor" ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="font-medium">历史轻量修复记录</p>
                        <Badge variant="outline">
                          {workflow.patch_executor_model_call_count ?? 0} 次调用
                        </Badge>
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        模型：{workflow.patch_executor_model ?? "未上报"}
                      </p>
                      <dl className="mt-3 grid grid-cols-3 gap-3">
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            总 token
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.patch_executor_token_usage?.total_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输入
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.patch_executor_token_usage?.input_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输出
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.patch_executor_token_usage
                                ?.output_tokens,
                            )}
                          </dd>
                        </div>
                      </dl>
                    </div>
                  ) : null}

                  {workflow?.repair_engine === "openhands" &&
                  (workflow.external_repair_model_call_count ?? 0) > 0 ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="font-medium">Bug Workbench OpenHands 修复</p>
                        <Badge variant="outline">
                          {workflow.external_repair_execution_status ??
                            "running"}
                        </Badge>
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        模型：
                        {workflow.external_repair_models?.length
                          ? workflow.external_repair_models.join("、")
                          : "等待 Bug Workbench 上报"}
                      </p>
                      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            总 token
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_repair_token_usage
                                ?.total_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            输入
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_repair_token_usage
                                ?.prompt_tokens,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground text-xs">
                            模型调用
                          </dt>
                          <dd className="mt-1 font-medium tabular-nums">
                            {formatTokenCount(
                              workflow.external_repair_model_call_count,
                            )}{" "}
                            次
                          </dd>
                        </div>
                      </dl>
                    </div>
                  ) : null}

                  {workflow?.ui_ownership_contract ? (
                    <div className="mt-5 rounded-xl border p-4 text-sm">
                      <p className="font-medium">
                        平台实现归属（后端证据合同）
                      </p>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        知识地图和专家结论只提供候选；以下状态仅由本轮源码证据链锁定。
                      </p>
                      <dl className="mt-3 grid gap-2 text-sm">
                        <div>
                          <dt className="text-muted-foreground inline">
                            状态：
                          </dt>
                          <dd className="inline">
                            {workflow.ui_ownership_contract.state ??
                              "unresolved"}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground inline">
                            已观察端：
                          </dt>
                          <dd className="inline">
                            {workflow.ui_ownership_contract.observed_clients
                              ?.length
                              ? workflow.ui_ownership_contract.observed_clients.join(
                                  "、",
                                )
                              : "无"}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-muted-foreground inline">
                            实现归属：
                          </dt>
                          <dd className="inline">
                            {workflow.ui_ownership_contract
                              .implementation_owner ?? "unknown"}
                          </dd>
                        </div>
                        {workflow.ui_ownership_contract.execution_anchor
                          ?.path ? (
                          <div>
                            <dt className="text-muted-foreground inline">
                              执行锚点：
                            </dt>
                            <dd className="inline">
                              {
                                workflow.ui_ownership_contract.execution_anchor
                                  .path
                              }
                              :
                              {
                                workflow.ui_ownership_contract.execution_anchor
                                  .line
                              }
                            </dd>
                          </div>
                        ) : null}
                        <div>
                          <dt className="text-muted-foreground inline">
                            修复目标：
                          </dt>
                          <dd className="inline">
                            {workflow.ui_ownership_contract.repair_targets
                              ?.length
                              ? workflow.ui_ownership_contract.repair_targets
                                  .map(
                                    (target) => `${target.path}:${target.line}`,
                                  )
                                  .join("、")
                              : "待补证据"}
                          </dd>
                        </div>
                      </dl>
                      {workflow.ui_ownership_contract.excluded_candidates
                        ?.length ? (
                        <p className="text-muted-foreground mt-3 text-xs">
                          排除候选：
                          {workflow.ui_ownership_contract.excluded_candidates
                            .map((item) => `${item.owner}（${item.reason}）`)
                            .join("；")}
                        </p>
                      ) : null}
                      {workflow.ui_ownership_contract.missing_evidence
                        ?.length ? (
                        <p className="text-muted-foreground mt-2 text-xs">
                          缺失证据：
                          {workflow.ui_ownership_contract.missing_evidence.join(
                            "、",
                          )}
                        </p>
                      ) : null}
                    </div>
                  ) : null}

                  {workflow?.attachment_evidence?.assets?.length ? (
                    <div className="mt-5 rounded-xl border p-4">
                      <p className="text-sm font-medium">禅道图片与附件证据</p>
                      <p className="text-muted-foreground mt-1 text-xs leading-5">
                        图片和日志会自动处理；视频仅在它是唯一有效附件且你确认后下载。附件失败不会中断原分析链路。
                      </p>
                      <ul className="mt-3 space-y-2">
                        {workflow.attachment_evidence.assets.map(
                          (asset, index) => (
                            <li
                              key={`${asset.id ?? asset.name}-${index}`}
                              className="bg-muted/20 flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm"
                            >
                              <span className="min-w-0 truncate">
                                {asset.name}
                              </span>
                              <Badge
                                variant={
                                  asset.status === "failed" ||
                                  asset.status === "type_mismatch"
                                    ? "destructive"
                                    : "outline"
                                }
                              >
                                {getBugEvidenceAssetStatusLabel(asset.status)}
                              </Badge>
                              {asset.error ? (
                                <span className="text-destructive w-full text-xs">
                                  {asset.error}
                                </span>
                              ) : null}
                            </li>
                          ),
                        )}
                      </ul>
                    </div>
                  ) : null}

                  {workflow?.status === "awaiting_clarification" &&
                  (workflow.clarification_type === "product" ||
                    workflow.clarification_type === "video") &&
                  workflow.clarification_stage === "pre_analysis" ? (
                    <ProductClarificationCard
                      clarification={workflow.clarification ?? {}}
                      clarificationType={workflow.clarification_type}
                      submitting={submittingClarification}
                      onSubmit={resumeWithClarification}
                    />
                  ) : null}

                  {workflow?.status === "awaiting_repair_choice" ? (
                    <div className="border-primary/30 bg-primary/5 mt-5 rounded-xl border p-4">
                      <p className="text-sm font-medium">旧版只读任务</p>
                      <p className="text-muted-foreground mt-2 text-sm leading-6">
                        这是优化前创建的历史任务，只保留分析结果查看，不再支持继续选择、补充证据或启动修复。请重新提交该
                        Bug ID 创建当前流程。
                      </p>
                    </div>
                  ) : null}

                  {workflow?.status === "awaiting_acceptance" ? (
                    <div className="border-primary/30 bg-primary/5 mt-5 rounded-xl border p-4">
                      <p className="text-sm font-medium">
                        历史修复已产生文件改动
                      </p>
                      <p className="text-muted-foreground mt-2 text-sm leading-6">
                        请先在你的本地环境验证结果。验证通过则记录验收；若问题未解决，可安全回退本次修复产生的文件改动。
                      </p>
                      <div className="mt-3 flex flex-wrap gap-3">
                        <Button
                          onClick={() => void finishRepair("accept")}
                          disabled={submittingAcceptance}
                        >
                          {submittingAcceptance ? "正在记录" : "验收通过"}
                        </Button>
                        <Button
                          variant="outline"
                          onClick={() => void finishRepair("rollback")}
                          disabled={
                            submittingAcceptance ||
                            !workflow.rollback?.available
                          }
                        >
                          回退本次修改
                        </Button>
                      </div>
                    </div>
                  ) : null}

                  {workflow?.status === "failed" &&
                  (workflow.failure_kind === "note_generation_failed" ||
                    workflow.failure_kind === "note_write_failed") &&
                  workflow.analysis_report ? (
                    <div className="border-primary/30 bg-primary/5 mt-5 rounded-xl border p-4">
                      <p className="text-sm font-medium">分析已完成，禅道备注待重试</p>
                      <p className="text-muted-foreground mt-2 text-sm leading-6">
                        已保存完整分析。重试只会重新生成备注，或复用已验证内容再次写入；不会重新运行源码调查。
                      </p>
                      <Button
                        className="mt-3"
                        onClick={() => void retryNote()}
                        disabled={retryingNote}
                      >
                        {retryingNote ? "正在重试" : "重试禅道备注"}
                      </Button>
                    </div>
                  ) : null}

                  {workflow?.last_event?.event_type === "repair_failed" && workflow.note_verified ? (
                    <p className="text-muted-foreground mt-5 rounded-xl border p-4 text-sm">
                      历史自动修复失败；原分析和已确认备注仍可查看。自动修复入口已停用。
                    </p>
                  ) : null}

                  <p className="text-muted-foreground mt-3 text-xs">
                    分析及修改／处理意见写入禅道并回读确认后完成。系统不修改代码、不提交 Git、不关闭或解决
                    Bug 或改动其他禅道字段。
                  </p>
                </div>

                <aside className="bg-card rounded-2xl border p-5 shadow-sm">
                  <div className="flex items-center gap-2 font-medium">
                    <GitPullRequest className="text-primary size-4" /> Bug
                    交付清单
                  </div>
                  <ul className="text-muted-foreground mt-4 space-y-3 text-sm">
                    <li className="flex gap-2">
                      <span className="text-primary">01</span>{" "}
                      分析结论包含已证实证据与待人工确认项。
                    </li>
                    <li className="flex gap-2">
                      <span className="text-primary">02</span>{" "}
                      系统提取报告第三、第四部分，并回读 Bug 动态确认。
                    </li>
                    <li className="flex gap-2">
                      <span className="text-primary">03</span>{" "}
                      备注同时包含必要证据和修改／处理意见，确认后完成；不会自动修复。
                    </li>
                  </ul>
                </aside>
              </section>

              <section className="bg-card rounded-2xl border p-5 shadow-sm">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 font-medium">
                    <ClipboardCheck className="text-primary size-4" />{" "}
                    {showingAnalysisDraft
                      ? "目标确认前的分析结果"
                      : workflow?.analysis_engine === "codex"
                        ? "Codex 四段分析结论"
                      : workflow?.analysis_engine === "openhands"
                        ? "Bug Workbench 分析结论"
                        : "完整专项分析报告"}
                  </div>
                  <Badge variant="outline">
                    {showingAnalysisDraft
                      ? "只读定位证据，不构成修改授权"
                      : workflow?.analysis_engine === "codex"
                        ? "四段中文报告 · 未修改代码"
                      : workflow?.analysis_engine === "openhands"
                        ? "四项中文结论"
                        : "八段完整报告，7000 字防失控上限"}
                  </Badge>
                </div>
                {workflow?.handoff_quality_warning ? (
                  <p className="mt-3 rounded-lg border border-amber-300/60 bg-amber-50/60 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
                    报告已完整保留，但长度或分段预算超出正常目标：共
                    {workflow.handoff_quality_warning.total_length}{" "}
                    字符；超额段落
                    {workflow.handoff_quality_warning.oversized_sections.length
                      ? `为 ${workflow.handoff_quality_warning.oversized_sections.map((item) => item.section).join("、")}`
                      : "无，总长度超过 4600"}
                    。
                  </p>
                ) : null}
                <pre className="text-muted-foreground bg-muted/20 mt-4 min-h-24 rounded-xl border border-dashed p-4 font-sans text-sm leading-6 break-words whitespace-pre-wrap">
                  {displayedHandoff ??
                    "分析专家完成后，交接摘要会自动显示在这里。"}
                </pre>
                {workflow?.analysis_engine === "openhands" &&
                workflow.triage ? (
                  <>
                    <p className="mt-5 flex items-center gap-2 font-medium">
                      <GitPullRequest className="text-primary size-4" />{" "}
                      初步问题方向
                    </p>
                    <pre className="text-muted-foreground bg-muted/20 mt-3 rounded-xl border border-dashed p-4 font-sans text-sm leading-6 break-words whitespace-pre-wrap">
                      {workflow.triage.display_name ?? "问题方向待源码确认"}
                      {workflow.triage.observed_clients?.length
                        ? ` · 已观察端：${workflow.triage.observed_clients.join("/")}`
                        : ""}
                      {`\n${workflow.triage.reason ?? "最终责任端与根因由源码调查确认。"}`}
                      {workflow.triage.direction
                        ? `\n调查提示：${workflow.triage.direction}`
                        : ""}
                      {"\n该分类只描述工单现象，不代表已确认源码责任。"}
                      {workflow.triage_model_call_count
                        ? `\n轻量语义判断：${workflow.triage_model_call_count} 次模型调用`
                        : ""}
                    </pre>
                  </>
                ) : workflow?.route_reason ? (
                  <>
                    <p className="mt-5 flex items-center gap-2 font-medium">
                      <GitPullRequest className="text-primary size-4" />{" "}
                      历史问题方向
                    </p>
                    <pre className="text-muted-foreground bg-muted/20 mt-3 rounded-xl border border-dashed p-4 font-sans text-sm leading-6 break-words whitespace-pre-wrap">
                      {workflow.route_reason}
                    </pre>
                  </>
                ) : null}
                {workflow?.note_content ? (
                  <>
                    <p className="mt-5 flex items-center gap-2 font-medium">
                      <ClipboardCheck className="text-primary size-4" />{" "}
                      {workflow.note_verified
                        ? "已确认写入的禅道备注"
                        : "尚未确认写入的禅道备注内容"}
                    </p>
                    <pre className="text-muted-foreground bg-muted/20 mt-3 max-h-96 overflow-auto rounded-xl border border-dashed p-4 font-sans text-sm leading-6 break-words whitespace-pre-wrap">
                      {workflow.note_content}
                    </pre>
                  </>
                ) : null}
                {workflow?.note_report ? (
                  <p className="text-muted-foreground mt-3 text-sm">
                    {workflow.note_report}
                  </p>
                ) : null}
                {workflow?.repair_report ? (
                  <>
                    <p className="mt-5 flex items-center gap-2 font-medium">
                      <GitPullRequest className="text-primary size-4" />{" "}
                      历史修复结果
                    </p>
                    <pre className="text-muted-foreground bg-muted/20 mt-3 max-h-96 overflow-auto rounded-xl border border-dashed p-4 font-sans text-sm leading-6 break-words whitespace-pre-wrap">
                      {workflow.repair_report}
                    </pre>
                    {workflow.changed_files?.length ? (
                      <p className="text-muted-foreground mt-3 text-sm">
                        实际修改文件：{workflow.changed_files.join("、")}
                      </p>
                    ) : null}
                  </>
                ) : null}
                {workflow?.completion_report ? (
                  <p className="text-muted-foreground mt-3 text-sm">
                    {workflow.completion_report}
                  </p>
                ) : null}
                {workflow?.error ? (
                  <p className="text-destructive mt-4 flex items-center gap-2 text-sm">
                    <TriangleAlert className="size-4" /> {workflow.error}
                  </p>
                ) : null}
              </section>
            </>
          ) : (
            <section className="bg-muted/20 rounded-2xl border border-dashed p-8 text-center">
              <FileSearch className="text-primary mx-auto size-8" />
              <h2 className="mt-3 font-medium">从一个禅道 Bug 开始</h2>
              <p className="text-muted-foreground mt-1 text-sm">
                输入 Bug ID 后，Bug Workbench
                完成连续只读调查，生成四段结论和修改／处理意见，写入禅道并回读确认后结束。
              </p>
            </section>
          )}

          {error ? (
            <p className="text-destructive border-destructive/40 bg-destructive/5 flex items-center gap-2 rounded-xl border p-4 text-sm">
              <TriangleAlert className="size-4" /> {error}
            </p>
          ) : null}
        </div>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
