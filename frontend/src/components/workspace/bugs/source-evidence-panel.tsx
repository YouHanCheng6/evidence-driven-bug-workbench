"use client";

import {
  Check,
  ChevronDown,
  CircleDot,
  FileCode2,
  Search,
  Sparkles,
} from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { type BugWorkflow } from "@/core/bugs/api";
import { cn } from "@/lib/utils";

type SourceRetrieval = NonNullable<BugWorkflow["source_retrieval"]>;

const rejectionLabels: Record<string, string> = {
  missing_path: "缺少文件路径",
  path_outside_repository: "路径不在当前仓库",
  path_not_found: "当前检出中不存在",
  stale_revision: "不是当前源码版本",
  empty_snippet: "没有可验真的源码片段",
  packet_budget: "超过事实包长度预算",
};

function compactRevision(value: string | undefined): string {
  if (!value) return "未记录";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function scoreLabel(value: number | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "未评分";
  return value <= 1 ? `${Math.round(value * 100)}%` : value.toFixed(2);
}

function stageLabel(value: string | undefined): string {
  if (value === "concept_fallback" || value === "concept_expansion") return "概念扩召回";
  if (value === "module_architecture") return "模块装配解析";
  return "精确锚点";
}

function semanticLabel(value: string | undefined): string {
  switch (value) {
    case "reranked":
    case "ready":
      return "已重排";
    case "disabled":
      return "未启用";
    case "unavailable":
    case "failed":
      return "重排不可用";
    default:
      return value ? value.replaceAll("_", " ") : "未运行";
  }
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="border-border/70 bg-background rounded-lg border px-3 py-2">
      <p className="text-muted-foreground text-[11px] leading-4">{label}</p>
      <p className="mt-0.5 text-sm font-semibold tabular-nums">{value}</p>
    </div>
  );
}

function EvidenceRail({ retrieval }: { retrieval: SourceRetrieval }) {
  const metrics = retrieval.metrics ?? {};
  const conceptCount = metrics.fallback_concept_count ?? 0;
  const stages = [
    {
      label: "精确锚点",
      value: `${metrics.anchor_count ?? 0} 个`,
      icon: Search,
      active: (metrics.anchor_count ?? 0) > 0,
    },
    {
      label: "概念扩召回",
      value: conceptCount > 0 ? `${conceptCount} 个` : "无可用概念",
      icon: CircleDot,
      active: conceptCount > 0,
    },
    {
      label: "Embedding 重排",
      value: semanticLabel(metrics.semantic_status),
      icon: Sparkles,
      active: semanticLabel(metrics.semantic_status) === "已重排",
    },
    {
      label: "当前源码验真",
      value: `${metrics.entry_count ?? retrieval.entries?.length ?? 0} 条`,
      icon: FileCode2,
      active: (metrics.entry_count ?? retrieval.entries?.length ?? 0) > 0,
    },
  ];

  return (
    <div className="relative grid gap-2 sm:grid-cols-4" aria-label="前置证据处理链路">
      <div className="bg-border absolute top-5 right-[12.5%] left-[12.5%] hidden h-px sm:block" />
      {stages.map((stage) => {
        const Icon = stage.icon;
        return (
          <div
            className="bg-background relative flex items-center gap-2 rounded-lg border px-3 py-2.5 sm:block sm:text-center"
            key={stage.label}
          >
            <span
              className={cn(
                "relative z-10 inline-flex size-6 shrink-0 items-center justify-center rounded-full border",
                stage.active
                  ? "border-emerald-600 bg-emerald-600 text-white"
                  : "border-border bg-muted text-muted-foreground",
              )}
            >
              {stage.active ? <Check className="size-3.5" /> : <Icon className="size-3.5" />}
            </span>
            <div className="sm:mt-2">
              <p className="text-xs font-medium">{stage.label}</p>
              <p className="text-muted-foreground mt-0.5 text-[11px]">{stage.value}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function SourceEvidencePanel({ retrieval }: { retrieval: SourceRetrieval }) {
  const [open, setOpen] = useState(false);
  const metrics = retrieval.metrics ?? {};
  const entries = retrieval.entries ?? [];
  const rejections = retrieval.rejections ?? [];
  const exactAnchors = metrics.exact_anchors ?? [];
  const fallbackConcepts = metrics.fallback_concepts ?? [];
  const ready = retrieval.status === "ready" && entries.length > 0;

  return (
    <Collapsible
      className="mt-5 overflow-hidden rounded-xl border"
      onOpenChange={setOpen}
      open={open}
    >
      <CollapsibleTrigger className="hover:bg-muted/35 flex w-full items-start justify-between gap-4 px-4 py-3 text-left transition-colors">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium">前置证据检索</p>
            <Badge
              className={cn(
                ready
                  ? "border-emerald-600/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                  : "",
              )}
              variant="outline"
            >
              {ready ? `已形成 ${entries.length} 条候选` : "未形成可信候选"}
            </Badge>
          </div>
          <p className="text-muted-foreground mt-1 text-xs leading-5">
            {retrieval.repository ?? "当前仓库"} · revision {compactRevision(retrieval.source_revision)} ·
            候选只用于导航，不代表已确认根因
          </p>
        </div>
        <ChevronDown
          className={cn("text-muted-foreground mt-0.5 size-4 shrink-0 transition-transform", open && "rotate-180")}
        />
      </CollapsibleTrigger>

      <CollapsibleContent>
        <div className="border-t px-4 py-4">
          <EvidenceRail retrieval={retrieval} />

          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Metric label="原始候选" value={metrics.raw_candidate_count ?? 0} />
            <Metric label="重排后" value={metrics.ranked_candidate_count ?? 0} />
            <Metric label="交给 Codex" value={metrics.entry_count ?? entries.length} />
            <Metric label="事实包字符" value={(metrics.packet_chars ?? 0).toLocaleString("zh-CN")} />
          </div>

          {exactAnchors.length || fallbackConcepts.length ? (
            <div className="mt-4 grid gap-3 lg:grid-cols-2">
              <div>
                <p className="text-muted-foreground text-xs font-medium">精确搜索锚点</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {exactAnchors.length ? exactAnchors.map((anchor) => (
                    <span className="bg-muted rounded-md px-2 py-1 font-mono text-[11px]" key={anchor}>{anchor}</span>
                  )) : <span className="text-muted-foreground text-xs">无</span>}
                </div>
              </div>
              <div>
                <p className="text-muted-foreground text-xs font-medium">概念扩召回</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {fallbackConcepts.length ? fallbackConcepts.map((concept) => (
                    <span className="border-border rounded-md border px-2 py-1 text-[11px]" key={concept}>{concept}</span>
                  )) : <span className="text-muted-foreground text-xs">无可用概念</span>}
                </div>
              </div>
            </div>
          ) : null}

          <div className="mt-4 space-y-2">
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs font-medium">当前源码候选</p>
              <span className="text-muted-foreground text-[11px]">按相关性排序，最多展示工作流保留的条目</span>
            </div>
            {entries.length ? entries.map((entry, index) => (
              <details className="group bg-muted/20 rounded-lg border px-3 py-2.5" key={`${entry.path ?? "candidate"}-${index}`}>
                <summary className="cursor-pointer list-none">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate font-mono text-xs font-medium">
                        {entry.path ?? "未知文件"}{entry.entry_line ? `:${entry.entry_line}` : ""}
                      </p>
                      <p className="text-muted-foreground mt-1 text-[11px]">
                        {stageLabel(entry.retrieval_stage)}
                        {entry.symbol ? ` · 命中 ${entry.symbol}` : ""}
                        {entry.product_scope && entry.product_scope !== "unspecified" ? ` · ${entry.product_scope}` : ""}
                      </p>
                    </div>
                    <Badge variant="secondary">相关度 {scoreLabel(entry.score)}</Badge>
                  </div>
                </summary>
                {entry.snippet ? (
                  <pre className="bg-background mt-3 max-h-64 overflow-auto rounded-md border p-3 text-[11px] leading-5 whitespace-pre-wrap">
                    <code>{entry.snippet}</code>
                  </pre>
                ) : null}
              </details>
            )) : (
              <div className="border-border bg-muted/20 rounded-lg border border-dashed px-3 py-4 text-xs leading-5">
                没有候选通过当前源码校验。Codex 会从工单事实直接窄查，不会把空召回包装成证据。
              </div>
            )}
          </div>

          {rejections.length ? (
            <details className="mt-4 text-xs">
              <summary className="text-muted-foreground cursor-pointer">查看 {rejections.length} 条淘汰记录</summary>
              <ul className="mt-2 space-y-1.5">
                {rejections.map((item, index) => (
                  <li className="flex gap-2" key={`${item.path ?? "rejection"}-${index}`}>
                    <span className="min-w-0 flex-1 truncate font-mono">{item.path || "未知路径"}</span>
                    <span className="text-muted-foreground shrink-0">{rejectionLabels[item.reason ?? ""] ?? item.reason ?? "未说明"}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
