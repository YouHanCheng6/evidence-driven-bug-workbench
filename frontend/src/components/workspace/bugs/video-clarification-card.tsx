"use client";

import { ArrowRight } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { type BugWorkflow } from "@/core/bugs/api";

export type VideoClarificationSubmission = { optionId: string };

export function VideoClarificationCard({
  clarification,
  submitting,
  onSubmit,
}: {
  clarification: NonNullable<BugWorkflow["clarification"]>;
  submitting: boolean;
  onSubmit: (submission: VideoClarificationSubmission) => Promise<void> | void;
}) {
  const [selectedOptionId, setSelectedOptionId] = useState("");

  useEffect(() => setSelectedOptionId(""), [clarification.question]);

  return (
    <div className="border-primary/30 bg-primary/5 mt-5 rounded-xl border p-4">
      <p className="text-sm font-medium">确认是否分析视频</p>
      <p className="text-muted-foreground mt-2 text-sm leading-6">
        {clarification.question ?? "视频是唯一有效附件，是否下载并分析？"}
      </p>
      <div className="mt-3 flex flex-wrap gap-2" role="radiogroup" aria-label="视频分析选项">
        {(clarification.options ?? []).slice(0, 2).map((option) => {
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
              {option.label}
            </Button>
          );
        })}
      </div>
      <p className="text-muted-foreground mt-3 text-xs">
        只有选择分析后才会下载、抽帧并读取视频内容。
      </p>
      <Button
        className="mt-3"
        onClick={() => void onSubmit({ optionId: selectedOptionId })}
        disabled={!selectedOptionId || submitting}
      >
        {submitting ? "正在提交" : "确认选择并继续"}
        <ArrowRight />
      </Button>
    </div>
  );
}
