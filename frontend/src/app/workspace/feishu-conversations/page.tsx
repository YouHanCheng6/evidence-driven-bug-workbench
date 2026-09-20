"use client";

import {
  Eraser,
  ExternalLink,
  LoaderCircle,
  MessageSquareMore,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";
import {
  clearFeishuConversationContext,
  deleteFeishuConversation,
  getFeishuConversationMessages,
  listFeishuConversations,
  type FeishuConversation,
  type FeishuTranscriptMessage,
} from "@/core/feishu-conversations/api";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

type PendingAction = "clear" | "delete" | null;
type ConversationView = "main_agent" | "bug_workbench";

function conversationKindLabel(conversation: FeishuConversation): string {
  return conversation.kind === "bug_workbench"
    ? "Bug 工作台任务"
    : "主 Agent 会话";
}

function workflowStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    routing: "正在分流",
    analyzing: "正在分析",
    awaiting_evidence: "旧任务只读",
    awaiting_clarification: "等待需求确认",
    writing_note: "正在写入备注",
    note_written: "已写入备注",
    skipped: "Bug 已结束，未分析",
    cancelled: "已停止",
    failed: "未完成",
  };
  return labels[status] ?? (status === "busy" ? "进行中" : "已结束");
}

function platformLabel(value: string): string {
  return { android: "Android", ios: "iOS", harmony: "Harmony" }[value] ?? value;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function textFromContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (typeof part === "string") return part;
        if (part && typeof part === "object" && "text" in part) {
          const text = (part as { text?: unknown }).text;
          return typeof text === "string" ? text : "";
        }
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if ("content" in record) return textFromContent(record.content);
    if (typeof record.text === "string") return record.text;
  }
  return "";
}

function messagePresentation(message: FeishuTranscriptMessage) {
  const record =
    message.content && typeof message.content === "object"
      ? (message.content as Record<string, unknown>)
      : null;
  const type = record?.type;
  const isHuman = type === "human";
  return {
    label: isHuman ? "飞书成员" : type === "tool" ? "工具" : "成有翰",
    isHuman,
    text:
      textFromContent(record?.content ?? message.content) || "（无可显示文本）",
  };
}

export default function FeishuConversationsPage() {
  const { t } = useI18n();
  const [conversations, setConversations] = useState<FeishuConversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<FeishuTranscriptMessage[]>([]);
  const [query, setQuery] = useState("");
  const [activeView, setActiveView] = useState<ConversationView>("main_agent");
  const [loading, setLoading] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction>(null);
  const [submittingAction, setSubmittingAction] = useState(false);

  const selected =
    conversations.find((item) => item.threadId === selectedId) ?? null;
  const searchedConversations = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return conversations;
    return conversations.filter((item) =>
      `${item.title} ${item.chatId ?? ""} ${item.topicId ?? ""}`
        .toLowerCase()
        .includes(normalized),
    );
  }, [conversations, query]);
  const visibleMainAgentConversations = useMemo(
    () => searchedConversations.filter((item) => item.kind === "main_agent"),
    [searchedConversations],
  );
  const visibleBugTasks = useMemo(
    () => searchedConversations.filter((item) => item.kind === "bug_workbench"),
    [searchedConversations],
  );
  const visibleConversations = useMemo(
    () =>
      activeView === "main_agent"
        ? visibleMainAgentConversations
        : visibleBugTasks,
    [activeView, visibleBugTasks, visibleMainAgentConversations],
  );

  useEffect(() => {
    setSelectedId((current) =>
      visibleConversations.some((item) => item.threadId === current)
        ? current
        : (visibleConversations[0]?.threadId ?? null),
    );
  }, [activeView, query, conversations, visibleConversations]);

  async function refreshConversations() {
    setLoading(true);
    setError(null);
    try {
      const next = await listFeishuConversations();
      setConversations(next);
      setSelectedId((current) =>
        next.some((item) => item.threadId === current)
          ? current
          : (next[0]?.threadId ?? null),
      );
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法读取飞书会话。",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    document.title = `${t.sidebar.feishuConversations} - ${t.pages.appName}`;
    void refreshConversations();
  }, [t.pages.appName, t.sidebar.feishuConversations]);

  useEffect(() => {
    if (!selectedId) {
      setMessages([]);
      return;
    }
    if (selected?.kind === "bug_workbench") {
      setMessages([]);
      setLoadingMessages(false);
      return;
    }
    setLoadingMessages(true);
    void getFeishuConversationMessages(selectedId)
      .then(setMessages)
      .catch((requestError) => {
        setMessages([]);
        toast.error(
          requestError instanceof Error
            ? requestError.message
            : "无法读取会话消息。",
        );
      })
      .finally(() => setLoadingMessages(false));
  }, [selected?.kind, selectedId]);

  async function confirmAction() {
    if (!selected || !pendingAction) return;
    setSubmittingAction(true);
    try {
      if (pendingAction === "clear") {
        await clearFeishuConversationContext(selected.threadId);
        setMessages([]);
        toast.success("已清空与有翰的聊天上下文。");
        await refreshConversations();
      } else {
        await deleteFeishuConversation(selected.threadId);
        setConversations((current) =>
          current.filter((item) => item.threadId !== selected.threadId),
        );
        setSelectedId(null);
        setMessages([]);
        toast.success("已删除本地飞书会话记录。");
      }
      setPendingAction(null);
    } catch (requestError) {
      toast.error(
        requestError instanceof Error ? requestError.message : "操作未完成。",
      );
    } finally {
      setSubmittingAction(false);
    }
  }

  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <main className="w-full max-w-7xl px-5 py-7 lg:px-8">
          <section className="border-border/80 bg-card relative overflow-hidden rounded-3xl border px-6 py-7 shadow-sm">
            <div className="absolute top-0 left-0 h-full w-1 bg-gradient-to-b from-sky-500 via-cyan-500 to-transparent" />
            <p className="text-muted-foreground flex items-center gap-2 pt-2 text-sm font-medium">
              <MessageSquareMore className="size-4" /> 飞书会话维护
            </p>
            <div className="mt-3 flex flex-wrap items-end justify-between gap-5">
              <div>
                <h1 className="text-3xl font-semibold tracking-tight">
                  成有翰的会话库
                </h1>
                <p className="text-muted-foreground mt-2 max-w-2xl text-sm leading-6">
                  查看当前账号可访问的飞书会话
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => void refreshConversations()}
                disabled={loading}
              >
                <RefreshCw
                  className={cn("size-4", loading && "animate-spin")}
                />{" "}
                刷新列表
              </Button>
            </div>
          </section>

          <section className="mt-6 flex min-h-[560px] flex-col gap-5 lg:flex-row">
            <aside className="bg-card w-full shrink-0 rounded-2xl border p-3 shadow-sm lg:w-90">
              <div className="relative">
                <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="pl-11"
                  placeholder="搜索标题或会话标识"
                />
              </div>
              <div className="bg-muted/60 mt-4 grid grid-cols-2 gap-2 rounded-xl p-1">
                <Button
                  type="button"
                  size="sm"
                  variant={activeView === "main_agent" ? "default" : "ghost"}
                  className="justify-center"
                  onClick={() => setActiveView("main_agent")}
                >
                  主 Agent
                  <span className="ml-1 text-xs opacity-70">
                    {visibleMainAgentConversations.length}
                  </span>
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={activeView === "bug_workbench" ? "default" : "ghost"}
                  className="justify-center"
                  onClick={() => setActiveView("bug_workbench")}
                >
                  Bug 工作台
                  <span className="ml-1 text-xs opacity-70">
                    {visibleBugTasks.length}
                  </span>
                </Button>
              </div>
              <p className="text-muted-foreground px-1 pt-3 pb-2 text-xs">
                {activeView === "main_agent"
                  ? "显示普通飞书聊天记录"
                  : "显示每一次 Bug 工作台运行记录"}
              </p>
              <div className="max-h-[530px] space-y-1 overflow-y-auto pr-1">
                {loading ? (
                  <div className="text-muted-foreground flex items-center justify-center gap-2 py-14 text-sm">
                    <LoaderCircle className="size-4 animate-spin" />
                    正在读取会话
                  </div>
                ) : visibleConversations.length ? (
                  <div className="pb-3">
                    {visibleConversations.map((conversation) => (
                      <button
                        key={conversation.threadId}
                        type="button"
                        onClick={() => setSelectedId(conversation.threadId)}
                        className={cn(
                          "w-full rounded-xl px-3 py-3 text-left transition-colors",
                          selectedId === conversation.threadId
                            ? "bg-primary text-primary-foreground"
                            : "hover:bg-muted",
                        )}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span className="truncate text-sm font-medium">
                            {conversation.title}
                          </span>
                          <Badge
                            variant={
                              conversation.status === "busy" ||
                              ["routing", "analyzing", "writing_note"].includes(
                                conversation.status,
                              )
                                ? "default"
                                : "secondary"
                            }
                            className="shrink-0 text-[10px]"
                          >
                            {workflowStatusLabel(conversation.status)}
                          </Badge>
                        </div>
                        <p
                          className={cn(
                            "mt-1 truncate font-mono text-[11px]",
                            selectedId === conversation.threadId
                              ? "text-primary-foreground/70"
                              : "text-muted-foreground",
                          )}
                        >
                          {conversation.kind === "bug_workbench"
                            ? `Bug #${conversation.bugTask?.bugId ?? ""}`
                            : (conversation.chatId ?? "飞书会话")}
                        </p>
                        <p
                          className={cn(
                            "mt-1 text-xs",
                            selectedId === conversation.threadId
                              ? "text-primary-foreground/70"
                              : "text-muted-foreground",
                          )}
                        >
                          {formatTime(conversation.updatedAt)}
                        </p>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-muted-foreground px-3 py-14 text-center text-sm">
                    {activeView === "main_agent"
                      ? "暂无可管理的主 Agent 会话。"
                      : "暂无 Bug 工作台任务记录。"}
                  </p>
                )}
              </div>
            </aside>

            <section className="bg-card min-h-[560px] min-w-0 flex-1 rounded-2xl border shadow-sm">
              {selected ? (
                <>
                  <div className="border-b px-5 py-4">
                    <div className="flex flex-wrap items-start justify-between gap-4">
                      <div className="min-w-0">
                        <p className="text-muted-foreground text-xs">
                          {conversationKindLabel(selected)}
                        </p>
                        <h2 className="mt-1 truncate text-xl font-semibold">
                          {selected.title}
                        </h2>
                        <p className="text-muted-foreground mt-1 font-mono text-xs">
                          {selected.threadId}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setPendingAction("clear")}
                          disabled={selected.status === "busy"}
                        >
                          <Eraser />
                          清空上下文
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          className="text-destructive hover:text-destructive"
                          onClick={() => setPendingAction("delete")}
                          disabled={selected.status === "busy"}
                        >
                          <Trash2 />
                          删除
                        </Button>
                      </div>
                    </div>
                  </div>
                  <div className="max-h-[520px] space-y-4 overflow-y-auto p-5">
                    {selected.kind === "bug_workbench" ? (
                      <section className="space-y-4">
                        <div className="grid gap-3 sm:grid-cols-2">
                          <div className="bg-muted/30 rounded-xl border p-4">
                            <p className="text-muted-foreground text-xs">
                              任务状态
                            </p>
                            <p className="mt-1 text-sm font-medium">
                              {workflowStatusLabel(selected.status)}
                            </p>
                          </div>
                          <div className="bg-muted/30 rounded-xl border p-4">
                            <p className="text-muted-foreground text-xs">
                              分析类型
                            </p>
                            <p className="mt-1 text-sm font-medium">
                              {selected.bugTask?.route ?? "等待分流"}
                            </p>
                          </div>
                        </div>
                        {selected.bugTask?.platformResolution ? (
                          <div className="bg-muted/20 rounded-xl border p-4">
                            <p className="text-muted-foreground text-xs font-medium">
                              平台确认
                            </p>
                            <p className="mt-2 text-sm leading-6">
                              {selected.bugTask.platformResolution.reportedClients
                                .map(platformLabel)
                                .join(" / ") || "等待确认"}
                              {selected.bugTask.platformResolution
                                .primaryRepository
                                ? ` · 主仓库 ${selected.bugTask.platformResolution.primaryRepository}`
                                : ""}
                            </p>
                          </div>
                        ) : null}
                        <div className="bg-muted/20 rounded-xl border p-4">
                          <p className="text-muted-foreground text-xs font-medium">
                            完整分析与修改／处理意见
                          </p>
                          <p className="mt-2 text-sm leading-6 whitespace-pre-wrap">
                            {selected.bugTask?.analysisReport ??
                              selected.bugTask?.handoff ??
                              selected.bugTask?.clarification ??
                              selected.bugTask?.error ??
                              "任务尚未生成可展示的结果。"}
                          </p>
                        </div>
                        {selected.bugTask ? (
                          <Button asChild variant="outline" size="sm">
                            <Link href={selected.bugTask.workbenchHref}>
                              打开 Bug 工作台
                              <ExternalLink className="size-4" />
                            </Link>
                          </Button>
                        ) : null}
                        <p className="text-muted-foreground text-xs leading-5">
                          这里保留每次 Bug
                          工作台运行的根任务；内部分析线程不会显示在会话库中。
                        </p>
                      </section>
                    ) : loadingMessages ? (
                      <div className="text-muted-foreground flex items-center justify-center gap-2 py-20 text-sm">
                        <LoaderCircle className="size-4 animate-spin" />
                        正在载入消息
                      </div>
                    ) : messages.length ? (
                      messages.map((message, index) => {
                        const display = messagePresentation(message);
                        return (
                          <article
                            key={`${message.created_at}-${index}`}
                            className={cn(
                              "max-w-[88%] rounded-2xl border px-4 py-3",
                              display.isHuman
                                ? "ml-auto border-sky-200 bg-sky-50 dark:border-sky-900 dark:bg-sky-950/30"
                                : "bg-muted/35",
                            )}
                          >
                            <div className="text-muted-foreground flex items-center justify-between gap-4 text-xs">
                              <span>{display.label}</span>
                              <time>{formatTime(message.created_at)}</time>
                            </div>
                            <p className="mt-2 text-sm leading-6 whitespace-pre-wrap">
                              {display.text}
                            </p>
                          </article>
                        );
                      })
                    ) : (
                      <div className="text-muted-foreground mx-auto max-w-sm py-20 text-center text-sm leading-6">
                        暂无可读取的消息记录。若这是服务重启前的旧会话，旧的内存运行日志无法恢复；之后的新消息会保存到数据库。
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div className="text-muted-foreground flex h-full min-h-[560px] flex-col items-center justify-center gap-3 p-8 text-center">
                  <MessageSquareMore className="size-8" />
                  <p className="font-medium">选择一个飞书会话</p>
                  <p className="max-w-xs text-sm leading-6">
                    在左侧查看与有翰的消息上下文。
                  </p>
                </div>
              )}
            </section>
          </section>
          {error ? (
            <p className="text-destructive mt-4 text-sm">{error}</p>
          ) : null}
        </main>
      </WorkspaceBody>

      <Dialog
        open={pendingAction !== null}
        onOpenChange={(open) =>
          !open && !submittingAction && setPendingAction(null)
        }
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {pendingAction === "clear"
                ? "清空与有翰的聊天上下文？"
                : "删除本地飞书会话？"}
            </DialogTitle>
            <DialogDescription>
              {pendingAction === "clear"
                ? "会保留会话条目和飞书来源，但会删除保存的消息、工具结果与上下文。"
                : "会删除与成有翰的对话条目、消息、工具结果与上下文。飞书原消息不会被撤回。"}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setPendingAction(null)}
              disabled={submittingAction}
            >
              取消
            </Button>
            <Button
              variant={pendingAction === "delete" ? "destructive" : "default"}
              onClick={() => void confirmAction()}
              disabled={submittingAction}
            >
              {submittingAction
                ? "正在处理"
                : pendingAction === "clear"
                  ? "确认清空"
                  : "确认删除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </WorkspaceContainer>
  );
}
