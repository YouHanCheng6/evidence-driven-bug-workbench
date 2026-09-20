import type { AgentThread } from "./types";
import { isThreadPinned, sortPinnedThreads } from "./utils";

const MAX_VISIBLE_THREADS = 200;
const modelCache = new WeakMap<object, ThreadListModel>();

// A Bug Workbench run creates a metadata-only `bug-workflow-*` root task plus
// short-lived implementation threads. They belong in the Bug Workbench's
// dedicated history, not in normal chat navigation: the root has no chat
// messages and would otherwise open an empty conversation.
const BUG_WORKBENCH_ROOT_PREFIX = "bug-workflow-";
const BUG_WORKBENCH_WORKER_PREFIXES = [
  "bug-batch-", // metadata-only main-agent batch; results live in its parent chat
  "bug-router-",
  "bug-analysis-",
  "bug-review-",
  "bug-repair-",
  "bug-note-",
] as const;

export type ThreadListModel = {
  byId: ReadonlyMap<string, AgentThread>;
  threads: readonly AgentThread[];
  displayedThreads: readonly AgentThread[];
  canLoadMore: boolean;
};

export function isBugWorkbenchThread(thread: AgentThread): boolean {
  return isBugWorkbenchRootThread(thread) || isBugWorkbenchWorkerThread(thread);
}

export function isBugWorkbenchRootThread(
  thread: Pick<AgentThread, "thread_id">,
): boolean {
  return thread.thread_id.startsWith(BUG_WORKBENCH_ROOT_PREFIX);
}

export function isBugWorkbenchWorkerThread(
  thread: Pick<AgentThread, "thread_id">,
): boolean {
  return BUG_WORKBENCH_WORKER_PREFIXES.some((prefix) =>
    thread.thread_id.startsWith(prefix),
  );
}

export function isRecentChatThread(
  thread: Pick<AgentThread, "thread_id">,
): boolean {
  return !isBugWorkbenchWorkerThread(thread);
}

export function buildThreadListModel(
  pages: readonly (readonly AgentThread[])[],
): ThreadListModel {
  const cacheKey = pages as object;
  const cached = modelCache.get(cacheKey);
  if (cached) return cached;

  const byId = new Map<string, AgentThread>();
  for (const page of pages) {
    for (const thread of page) {
      if (!byId.has(thread.thread_id)) {
        byId.set(thread.thread_id, thread);
      }
    }
  }
  const threads = [...byId.values()];
  const sortedThreads = sortPinnedThreads(threads);
  const pinnedThreads = sortedThreads.filter(isThreadPinned);
  const recentThreads = sortedThreads
    .filter((thread) => !isThreadPinned(thread))
    .slice(0, MAX_VISIBLE_THREADS);
  const model: ThreadListModel = {
    byId,
    threads: sortedThreads,
    displayedThreads: [...pinnedThreads, ...recentThreads],
    canLoadMore: recentThreads.length < MAX_VISIBLE_THREADS,
  };
  modelCache.set(cacheKey, model);
  return model;
}
