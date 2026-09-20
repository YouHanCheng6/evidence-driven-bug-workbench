import { type BugWorkflow } from "@/core/bugs/api";

export type BugWorkbenchStage = "ready" | "analysis_requested" | "note_written";

export type BugEvidenceAssetStatus =
  | "queued"
  | "downloading"
  | "downloaded"
  | "processed"
  | "awaiting_confirmation"
  | "skipped"
  | "type_mismatch"
  | "failed";

export function getBugEvidenceAssetStatusLabel(
  status: BugEvidenceAssetStatus,
): string {
  switch (status) {
    case "queued":
      return "等待下载";
    case "downloading":
      return "正在下载";
    case "downloaded":
      return "已下载";
    case "processed":
      return "已作为分析证据";
    case "awaiting_confirmation":
      return "等待确认是否分析";
    case "skipped":
      return "已跳过";
    case "type_mismatch":
      return "文件类型异常";
    case "failed":
      return "下载失败";
  }
}

export function getBugWorkflowStatusLabel(
  status: BugWorkflow["status"] | undefined,
  clarificationType: BugWorkflow["clarification_type"] | undefined,
  repairAttempted = false,
  failureKind?: string | null,
): string {
  switch (status) {
    case "routing":
      return "Bug Workbench 正在整理事实";
    case "analyzing":
      return "Bug Workbench 正在调查源码";
    case "awaiting_evidence":
      return "旧任务只读";
    case "awaiting_clarification":
      if (clarificationType === "product") return "等待确认产品目标";
      if (clarificationType === "video") return "等待确认是否分析视频";
      return "旧任务只读";
    case "writing_note":
      return "正在写入禅道备注";
    case "analysis_incomplete":
      return "分析完成，仅人工处理";
    case "awaiting_repair_choice":
      return "旧版只读任务";
    case "note_written":
      return repairAttempted ? "历史修复已结束，零 diff" : "分析与建议已交付";
    case "repairing":
      return "历史任务处于修复状态";
    case "awaiting_acceptance":
      return "等待你的验收";
    case "accepted":
      return "验收已完成";
    case "rolled_back":
      return "已回退本次修复";
    case "skipped":
      return "Bug 已解决，未启动分析";
    case "cancelled":
      return "分析已取消";
    case "failed":
      if (failureKind === "analysis_execution_failed") return "Codex 源码调查失败";
      if (failureKind === "note_write_failed") return "分析已完成，禅道回写失败";
      if (failureKind === "note_generation_failed") return "分析已完成，备注生成失败";
      return "流程失败";
    default:
      return "正在创建流程";
  }
}

export function getOpenHandsInvestigationStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case "finished":
      return "Agent 主动收口";
    case "evidence_review_closed":
      return "证据检查点收口";
    case "evidence_reviewing":
      return "正在核对收口证据";
    case "external_boundary_closed":
      return "外部边界收口";
    case "max_iterations_reached":
      return "达到调用上限";
    case "provider_empty_interrupted":
      return "连续空响应中断";
    case "process_only_interrupted":
      return "过程性回复提前结束";
    case "stall_interrupted":
      return "无进展超时中断";
    case "request_timeout_interrupted":
      return "Sol 请求超时中断";
    case "request_timeout_retrying":
      return "Sol 超时后续查";
    case "external_interrupted":
      return "外部暂停或取消";
    case "native_stuck":
      return "OpenHands 原生停滞";
    case "running":
      return "调查中";
    case "connecting":
    case "prepared":
    case "navigation_prepared":
      return "准备中";
    default:
      return status ?? "连接中";
  }
}

export function getBugWorkflowStep(
  status: BugWorkflow["status"] | undefined,
  hasTask: boolean,
  repairAttempted = false,
): number {
  if (!hasTask) return -1;
  if (status === "routing") return 1;
  if (
    status === "awaiting_evidence" ||
    status === "awaiting_clarification" ||
    status === "analyzing" ||
    status === "analysis_incomplete"
  )
    return 2;
  if (status === "writing_note") return 4;
  if (status === "note_written") return 5;
  if (status === "awaiting_repair_choice") return 3;
  if (
    status === "repairing" ||
    status === "awaiting_acceptance" ||
    status === "accepted" ||
    status === "rolled_back"
  )
    return 5;
  return 0;
}

export function normalizeBugId(value: string): string | null {
  const match = /^(?:#|bug\s*#?)?(\d+)$/i.exec(value.trim());
  return match?.[1] ?? null;
}

export function resolveBugWorkflowId(
  routeWorkflowId: string | null,
  rememberedWorkflowId: string | null,
): string | null {
  return routeWorkflowId ?? rememberedWorkflowId;
}

export function getBugWorkbenchStage(input: {
  hasBugId: boolean;
  noteWritten: boolean;
}): BugWorkbenchStage {
  if (input.noteWritten) {
    return "note_written";
  }
  return input.hasBugId ? "analysis_requested" : "ready";
}
