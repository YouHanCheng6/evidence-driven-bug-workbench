import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

type ThreadSearchRecord = {
  thread_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  metadata?: Record<string, unknown>;
  values?: Record<string, unknown>;
};

export type FeishuConversation = {
  threadId: string;
  kind: "main_agent" | "bug_workbench";
  title: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  chatId: string | null;
  topicId: string | null;
  bugTask?: {
    bugId: number;
    route: string | null;
    handoff: string | null;
    noteContent: string | null;
    noteReport: string | null;
    analysisReport: string | null;
    platformResolution: {
      reportedClients: string[];
      primaryRepository: string | null;
      investigationMode: string | null;
    } | null;
    finalTargets: Array<Record<string, unknown>>;
    openEdges: string[];
    noteVerified: boolean | null;
    noteWriteSkipped: boolean;
    workbenchHref: string;
    error: string | null;
    clarification: string | null;
  };
};

export type FeishuTranscriptMessage = {
  content: unknown;
  created_at: string;
};

async function readError(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) return body.detail;
  } catch {
    // Use the caller's concise fallback when the response isn't JSON.
  }
  return fallback;
}

function feishuSource(metadata: Record<string, unknown> | undefined) {
  const source = metadata?.channel_source;
  if (!source || typeof source !== "object" || Array.isArray(source))
    return null;
  const value = source as Record<string, unknown>;
  return value.type === "im_channel" && value.provider === "feishu"
    ? value
    : null;
}

function feishuChatIdFromWorkflowKey(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const parts = value.split("\u001f");
  return parts[0] === "feishu" && typeof parts[2] === "string"
    ? parts[2] || null
    : null;
}

function bugWorkflow(metadata: Record<string, unknown> | undefined) {
  const workflow = metadata?.bug_workflow;
  if (!workflow || typeof workflow !== "object" || Array.isArray(workflow))
    return null;
  const value = workflow as Record<string, unknown>;
  const bugId = value.bug_id;
  const channelKey = metadata?.bug_workflow_channel_key ?? value.channel_key;
  if (
    typeof bugId !== "number" ||
    !Number.isInteger(bugId) ||
    !feishuChatIdFromWorkflowKey(channelKey)
  ) {
    return null;
  }
  const clarification = value.clarification;
  const platform =
    value.platform_resolution &&
    typeof value.platform_resolution === "object" &&
    !Array.isArray(value.platform_resolution)
      ? (value.platform_resolution as Record<string, unknown>)
      : null;
  const strings = (candidate: unknown): string[] =>
    Array.isArray(candidate)
      ? candidate.filter(
          (item): item is string =>
            typeof item === "string" && item.trim().length > 0,
        )
      : [];
  const openEdges = [
    ...strings(value.investigation_open_edges),
    ...strings(value.investigation_missing_evidence),
  ].filter((item, index, items) => items.indexOf(item) === index);
  return {
    bugId,
    chatId: feishuChatIdFromWorkflowKey(channelKey),
    route: typeof value.route === "string" ? value.route : null,
    status: typeof value.status === "string" ? value.status : null,
    handoff: typeof value.handoff === "string" ? value.handoff : null,
    noteContent:
      typeof value.note_content === "string" ? value.note_content : null,
    noteReport:
      typeof value.note_report === "string" ? value.note_report : null,
    analysisReport:
      typeof value.analysis_report === "string" ? value.analysis_report : null,
    platformResolution: platform
      ? {
          reportedClients: strings(platform.reported_clients),
          primaryRepository:
            typeof platform.primary_repository === "string"
              ? platform.primary_repository
              : null,
          investigationMode:
            typeof platform.investigation_mode === "string"
              ? platform.investigation_mode
              : null,
        }
      : null,
    finalTargets: Array.isArray(value.final_targets)
      ? value.final_targets.filter(
          (item): item is Record<string, unknown> =>
            Boolean(item) && typeof item === "object" && !Array.isArray(item),
        )
      : [],
    openEdges,
    noteVerified:
      typeof value.note_verified === "boolean" ? value.note_verified : null,
    noteWriteSkipped: value.note_write_skipped === true,
    error: typeof value.error === "string" ? value.error : null,
    clarification:
      clarification && typeof clarification === "object"
        ? typeof (clarification as Record<string, unknown>).question ===
          "string"
          ? ((clarification as Record<string, unknown>).question as string)
          : null
        : null,
  };
}

export function projectFeishuThread(
  thread: ThreadSearchRecord,
): FeishuConversation | null {
  const task = bugWorkflow(thread.metadata);
  if (task) {
    return {
      threadId: thread.thread_id,
      kind: "bug_workbench",
      title: `ZenTao Bug #${task.bugId}`,
      status: task.status ?? thread.status,
      createdAt: thread.created_at,
      updatedAt: thread.updated_at,
      chatId: task.chatId,
      topicId: null,
      bugTask: {
        bugId: task.bugId,
        route: task.route,
        handoff: task.handoff,
        noteContent: task.noteContent,
        noteReport: task.noteReport,
        analysisReport: task.analysisReport,
        platformResolution: task.platformResolution,
        finalTargets: task.finalTargets,
        openEdges: task.openEdges,
        noteVerified: task.noteVerified,
        noteWriteSkipped: task.noteWriteSkipped,
        workbenchHref: `/workspace/bugs?workflow=${encodeURIComponent(thread.thread_id)}`,
        error: task.error,
        clarification: task.clarification,
      },
    };
  }
  const source = feishuSource(thread.metadata);
  if (!source) return null;
  return {
    threadId: thread.thread_id,
    kind: "main_agent",
    title:
      typeof thread.values?.title === "string" && thread.values.title.trim()
        ? thread.values.title
        : "未命名飞书会话",
    status: thread.status,
    createdAt: thread.created_at,
    updatedAt: thread.updated_at,
    chatId: typeof source.chat_id === "string" ? source.chat_id : null,
    topicId: typeof source.topic_id === "string" ? source.topic_id : null,
  };
}

export async function listFeishuConversations(): Promise<FeishuConversation[]> {
  const response = await fetchWithAuth(
    `${getBackendBaseURL()}/api/threads/feishu-conversations`,
  );
  if (!response.ok) {
    throw new Error(await readError(response, "无法读取飞书会话。"));
  }

  const threads = (await response.json()) as ThreadSearchRecord[];
  return threads.flatMap((thread) => {
    const conversation = projectFeishuThread(thread);
    return conversation ? [conversation] : [];
  });
}

export async function getFeishuConversationMessages(
  threadId: string,
): Promise<FeishuTranscriptMessage[]> {
  const response = await fetchWithAuth(
    `${getBackendBaseURL()}/api/threads/feishu-conversations/${encodeURIComponent(threadId)}/messages`,
  );
  if (!response.ok) {
    throw new Error(await readError(response, "无法读取会话消息。"));
  }
  return (await response.json()) as FeishuTranscriptMessage[];
}

export async function clearFeishuConversationContext(
  threadId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${getBackendBaseURL()}/api/threads/feishu-conversations/${encodeURIComponent(threadId)}/clear-context`,
    { method: "POST" },
  );
  if (!response.ok) {
    throw new Error(await readError(response, "无法清空会话上下文。"));
  }
}

export async function deleteFeishuConversation(
  threadId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    `${getBackendBaseURL()}/api/threads/feishu-conversations/${encodeURIComponent(threadId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(await readError(response, "无法删除会话。"));
  }
}
