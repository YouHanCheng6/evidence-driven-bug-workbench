"use client";

import { ArrowRight } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { type BugWorkflow } from "@/core/bugs/api";

export type ProductClarificationSubmission = {
  answer?: string;
  optionId?: string;
};

export function ProductClarificationCard({
  clarification,
  clarificationType = "product",
  submitting,
  onSubmit,
}: {
  clarification: NonNullable<BugWorkflow["clarification"]>;
  clarificationType?: BugWorkflow["clarification_type"];
  submitting: boolean;
  onSubmit: (
    submission: ProductClarificationSubmission,
  ) => Promise<void> | void;
}) {
  const [selectedOptionId, setSelectedOptionId] = useState("");
  const [exactText, setExactText] = useState("");
  const mode =
    clarification.response_mode ??
    (clarification.options?.length ? "choice" : "exact_text");
  const isVideo = clarificationType === "video";

  useEffect(() => {
    setSelectedOptionId("");
    setExactText("");
  }, [clarification.question, mode]);

  return (
    <div className="border-primary/30 bg-primary/5 mt-5 rounded-xl border p-4">
      <p className="text-sm font-medium">
        {isVideo ? "确认是否分析视频" : "确认本次修改目标"}
      </p>
      <p className="text-muted-foreground mt-2 text-sm leading-6">
        {clarification.question ?? "Bug Workbench 需要确认最终用户可见结果。"}
      </p>
      {clarification.decision_reason ? (
        <p className="text-muted-foreground mt-2 text-xs leading-5">
          需要选择的原因：{clarification.decision_reason}
        </p>
      ) : null}
      {clarification.decision_impact ? (
        <p className="text-muted-foreground mt-1 text-xs leading-5">
          选择影响：{clarification.decision_impact}
        </p>
      ) : null}

      {mode === "copy_scope" ? (
        <>
          {(clarification.items ?? []).length ? (
            <ol className="mt-3 space-y-2">
              {(clarification.items ?? []).map((item, index) => (
                <li
                  key={item.id}
                  className="bg-muted/30 rounded-lg border px-3 py-2 text-sm"
                >
                  <p className="font-medium">
                    {index + 1}.{" "}
                    {item.observed_clients?.length
                      ? item.observed_clients
                          .map((client) =>
                            client === "ios"
                              ? "iOS"
                              : client === "android"
                                ? "Android"
                                : client === "harmony"
                                  ? "Harmony"
                                  : client,
                          )
                          .join("/")
                      : "客户端待确认"}
                    {item.page ? ` · ${item.page}` : ""}
                  </p>
                  <p className="text-muted-foreground mt-1">
                    当前：{item.actual_text ?? "未提取"}
                  </p>
                  <p className="text-muted-foreground mt-1">
                    参考：{item.expected_text ?? "未提供"}
                  </p>
                </li>
              ))}
            </ol>
          ) : null}
          <label
            className="mt-4 block text-sm font-medium"
            htmlFor="bug-copy-scope"
          >
            {clarification.input_label ?? "请明确本次文案修改范围"}
          </label>
          <p className="text-muted-foreground mt-1 text-xs">
            {clarification.input_hint ??
              "请写明修改编号和目标文案、明确不改的编号；跨端不一致时说明统一基准。"}
          </p>
          <Textarea
            id="bug-copy-scope"
            className="mt-2 min-h-28"
            value={exactText}
            maxLength={2000}
            onChange={(event) => setExactText(event.target.value)}
            placeholder={
              clarification.copy_scope_kind === "harmony"
                ? "例如：修改：1→新文案；不改：2、3"
                : "例如：修改：1→离家守护启用；不改：2、3；跨端基准：仅 Android"
            }
          />
          <Button
            className="mt-3"
            onClick={() => void onSubmit({ answer: exactText.trim() })}
            disabled={!exactText.trim() || submitting}
          >
            {submitting ? "正在提交" : "确认范围并开始分析"}
            <ArrowRight />
          </Button>
        </>
      ) : mode === "choice" ? (
        <>
          <div
            className="mt-3 flex flex-wrap gap-2"
            role="radiogroup"
            aria-label={isVideo ? "视频分析选项" : "产品目标选项"}
          >
            {(clarification.options ?? [])
              .slice(0, 4)
              .map((option, optionIndex) => {
                const selected = selectedOptionId === option.id;
                return (
                  <Button
                    key={option.id}
                    type="button"
                    size="sm"
                    variant={selected ? "default" : "outline"}
                    role="radio"
                    aria-checked={selected}
                    onClick={() => setSelectedOptionId(option.id)}
                    disabled={submitting}
                  >
                    {String.fromCharCode(65 + optionIndex)}. {option.label}
                  </Button>
                );
              })}
          </div>
          <p className="text-muted-foreground mt-3 text-xs">
            {isVideo
              ? "只有确认后才会下载、抽帧并分析视频。"
              : "请选择一个明确结果。系统会提交固定选项，不会解析补充描述。"}
          </p>
          <Button
            className="mt-3"
            onClick={() => void onSubmit({ optionId: selectedOptionId })}
            disabled={!selectedOptionId || submitting}
          >
            {submitting ? "正在提交" : "确认选择并继续"}
            <ArrowRight />
          </Button>
        </>
      ) : (
        <>
          {clarification.current_value ? (
            <p className="bg-muted/30 mt-3 rounded-lg border px-3 py-2 text-sm">
              当前文案：{clarification.current_value}
            </p>
          ) : null}
          <label
            className="mt-4 block text-sm font-medium"
            htmlFor="bug-exact-target-text"
          >
            {clarification.input_label ?? "请输入修改后的完整文案"}
          </label>
          <p className="text-muted-foreground mt-1 text-xs">
            {clarification.input_hint ??
              "只填写最终显示内容，不要描述修改方式或技术实现。"}
          </p>
          <Textarea
            id="bug-exact-target-text"
            className="mt-2 min-h-24"
            value={exactText}
            maxLength={2000}
            onChange={(event) => setExactText(event.target.value)}
            placeholder="例如：连接蓝牙设备"
          />
          {exactText.trim() ? (
            <p
              className="bg-muted/30 mt-3 rounded-lg border px-3 py-2 text-sm"
              aria-live="polite"
            >
              确认目标：
              {clarification.current_value
                ? `${clarification.current_value} → `
                : ""}
              {exactText.trim()}
            </p>
          ) : null}
          <Button
            className="mt-3"
            onClick={() => void onSubmit({ answer: exactText.trim() })}
            disabled={!exactText.trim() || submitting}
          >
            {submitting ? "正在提交" : "确认文案并继续"}
            <ArrowRight />
          </Button>
        </>
      )}
    </div>
  );
}
