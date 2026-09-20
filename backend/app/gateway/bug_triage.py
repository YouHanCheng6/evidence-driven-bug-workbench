"""Whole-ticket semantic intake contract for Bug Workbench.

Triage preserves the ticket as one investigation.  It may identify acceptance
details, hypotheses, and open edges, but it never freezes final targets,
implementation ownership, repairability, repair files, or root cause.  Final
targets are a result of source and runtime investigation, not an input to it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

BugProblemType = Literal["ui", "crash", "api", "unknown"]
UiProblemSubtype = Literal["copy", "layout_behavior", "visual", "interaction", "unknown"]
TriageConfidence = Literal["high", "medium", "low"]
EvidenceNeed = Literal["visual_only", "include_technical"]
TargetSource = Literal["expected", "title", "description", "steps_or_actual", "attachment"]
ObservedOutcome = Literal["mismatch", "satisfied", "unknown"]
TargetKind = Literal["copy", "visual", "layout", "interaction", "dynamic_data", "api_result", "crash", "unknown"]
InvestigationPolicy = Literal["resource_chain", "existing_interaction_chain", "data_transaction_chain", "crash_chain", "consumer_chain"]
RepairPolicy = Literal["resource_reuse_or_add", "reconnect_existing_only", "repair_existing", "report_if_absent"]

_SNAPSHOT_FIELDS = ("title", "description", "steps", "actual", "expected", "product", "client", "os", "version")
_CLIENT_PATTERNS = (
    ("android", re.compile(r"(?:\bAndroid(?![A-Za-z])|安卓)", re.IGNORECASE)),
    ("ios", re.compile(r"(?:\biOS(?![A-Za-z])|\bIOS(?![A-Za-z])|苹果(?:端|系统)?)", re.IGNORECASE)),
    ("harmony", re.compile(r"(?:\bHarmony(?:OS)?\b|鸿蒙)", re.IGNORECASE)),
)
_TECHNICAL_PRODUCT_DECISION_PATTERN = re.compile(
    r"(?:文件|路径|目录|代码|类名|方法名|变量|资源\s*key|调用链|调用点|日志|堆栈|"
    r"\b(?:xml|json|swift|objc|java|kotlin)\b|(?:[A-Za-z0-9_.-]+/){1,}[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BugInvestigationTarget:
    """Legacy pre-investigation target retained only for stored-run decoding."""

    target_id: str
    actual_behavior: str
    target_behavior: str
    fact_basis: str
    target_source: TargetSource
    evidence_focus: str
    observed_outcome: ObservedOutcome
    target_kind: TargetKind = "unknown"
    investigation_policy: InvestigationPolicy = "consumer_chain"
    repair_policy: RepairPolicy = "repair_existing"

    def payload(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "actual_behavior": self.actual_behavior,
            "target_behavior": self.target_behavior,
            "fact_basis": self.fact_basis,
            "target_source": self.target_source,
            "evidence_focus": self.evidence_focus,
            "observed_outcome": self.observed_outcome,
            "target_kind": self.target_kind,
            "investigation_policy": self.investigation_policy,
            "repair_policy": self.repair_policy,
        }


@dataclass(frozen=True, slots=True)
class BugInvestigationFocus:
    """One mutable whole-ticket investigation seed, never a final target list."""

    scenario: str
    observed_behavior: str
    expected_behavior: str
    acceptance_details: tuple[str, ...]
    initial_hypotheses: tuple[str, ...]
    open_edges: tuple[str, ...]
    evidence_focuses: tuple[str, ...]
    observed_outcome: ObservedOutcome

    def payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "observed_behavior": self.observed_behavior,
            "expected_behavior": self.expected_behavior,
            "acceptance_details": list(self.acceptance_details),
            "initial_hypotheses": list(self.initial_hypotheses),
            "open_edges": list(self.open_edges),
            "evidence_focuses": list(self.evidence_focuses),
            "observed_outcome": self.observed_outcome,
        }


@dataclass(frozen=True, slots=True)
class BugProductOption:
    """One mutually exclusive product result, not a technical implementation."""

    option_id: str
    label: str
    target_behavior: str

    def payload(self) -> dict[str, str]:
        return {
            "id": self.option_id,
            "label": self.label,
            "value": self.target_behavior,
        }


@dataclass(frozen=True, slots=True)
class BugProductDecision:
    """A real product fork whose choice changes behavior or modification scope."""

    question: str
    reason: str
    impact: str
    options: tuple[BugProductOption, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "reason": self.reason,
            "impact": self.impact,
            "options": [option.payload() for option in self.options],
        }


@dataclass(frozen=True, slots=True)
class BugTriageResult:
    """A bounded symptom classification, not a source-ownership conclusion."""

    problem_type: BugProblemType
    ui_subtype: UiProblemSubtype | None
    observed_clients: tuple[str, ...]
    confidence: TriageConfidence
    reason: str
    direction: str
    evidence_need: EvidenceNeed
    investigation_focus: BugInvestigationFocus | None = None
    # Historical workflows serialized this field.  New workflows always leave
    # it empty and derive final targets after evidence collection.
    investigation_targets: tuple[BugInvestigationTarget, ...] = ()
    product_decision: BugProductDecision | None = None

    @property
    def needs_product_confirmation(self) -> bool:
        return self.product_decision is not None

    @property
    def display_name(self) -> str:
        if self.problem_type == "crash":
            return "崩溃问题"
        if self.problem_type == "api":
            return "接口或状态问题"
        if self.problem_type == "ui" and self.ui_subtype == "copy":
            return "UI 文案问题"
        if self.problem_type == "ui" and self.ui_subtype == "layout_behavior":
            return "UI 布局或行为问题"
        if self.problem_type == "ui":
            return "UI 问题"
        return "问题方向待源码确认"

    def payload(self) -> dict[str, Any]:
        return {
            "problem_type": self.problem_type,
            "ui_subtype": self.ui_subtype,
            "observed_clients": list(self.observed_clients),
            "confidence": self.confidence,
            "reason": self.reason,
            "direction": self.direction,
            "evidence_need": self.evidence_need,
            "investigation_focus": self.investigation_focus.payload() if self.investigation_focus is not None else None,
            "product_decision": self.product_decision.payload() if self.product_decision is not None else None,
            "needs_product_confirmation": self.needs_product_confirmation,
            "display_name": self.display_name,
        }


def _snapshot_text(snapshot: Mapping[str, Any]) -> str:
    return " ".join(re.sub(r"\s+", " ", str(snapshot.get(field) or "")).strip() for field in _SNAPSHOT_FIELDS if snapshot.get(field) not in (None, ""))


def observed_clients(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract only explicit client names; this is not symptom classification."""
    text = _snapshot_text(snapshot)
    values = [name for name, pattern in _CLIENT_PATTERNS if pattern.search(text)]
    if "harmony" in values:
        return ("harmony",)
    return tuple(dict.fromkeys(values))




def _required_contract_text(value: Any, field: str, *, limit: int = 700) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if not compact:
        raise ValueError(f"triage {field} is required")
    return compact[:limit]








def _fallback_investigation_focus(snapshot: Mapping[str, Any]) -> BugInvestigationFocus:
    actual = snapshot.get("actual") or snapshot.get("description") or snapshot.get("title") or "工单记录的异常现象"
    if snapshot.get("expected"):
        target = snapshot["expected"]
    elif snapshot.get("title"):
        target = snapshot["title"]
    elif snapshot.get("description"):
        target = snapshot["description"]
    else:
        target = "恢复工单所描述场景下的正常业务行为"
    scenario = snapshot.get("steps") or snapshot.get("description") or snapshot.get("title") or "工单记录的用户场景"
    details = [str(snapshot.get(field) or "").strip() for field in ("expected", "description", "title")]
    return BugInvestigationFocus(
        scenario=_required_contract_text(scenario, "fallback scenario", limit=1_000),
        observed_behavior=_required_contract_text(actual, "fallback observed_behavior"),
        expected_behavior=_required_contract_text(target, "fallback expected_behavior"),
        acceptance_details=tuple(dict.fromkeys(value for value in details if value)) or (_required_contract_text(target, "fallback acceptance_detail"),),
        initial_hypotheses=(),
        open_edges=("最终消费者到状态、资源、交互或请求来源之间的实际连接",),
        evidence_focuses=("该异常行为的来源、最终消费者与现有业务实现",),
        observed_outcome=(
            "mismatch"
            if snapshot.get("actual") not in (None, "") and snapshot.get("expected") not in (None, "") and re.sub(r"\s+", " ", str(snapshot["actual"])).strip().casefold() != re.sub(r"\s+", " ", str(snapshot["expected"])).strip().casefold()
            else "unknown"
        ),
    )




def unknown_triage(snapshot: Mapping[str, Any], reason: str) -> BugTriageResult:
    """Keep investigation open when the semantic direction call is unavailable."""
    return BugTriageResult(
        problem_type="unknown",
        ui_subtype=None,
        observed_clients=observed_clients(snapshot),
        confidence="low",
        reason=reason,
        direction="从工单事实和地图候选开始核对本地行为链，由源码证据确定最终责任。",
        evidence_need="include_technical",
        investigation_focus=_fallback_investigation_focus(snapshot),
    )


def apply_visual_copy_evidence(
    triage: Mapping[str, Any],
    attachment_evidence: Mapping[str, Any] | None,
    *,
    harmony_reported: bool,
) -> dict[str, Any]:
    """Supplement UI direction with visual facts, never client routing."""
    del harmony_reported
    result = dict(triage)
    visual = attachment_evidence.get("visual_evidence") if isinstance(attachment_evidence, Mapping) else None
    items = visual.get("items") if isinstance(visual, Mapping) else None
    visual_items = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
    has_copy_difference = False
    visual_facts: list[str] = []
    for item in visual_items:
        actual = str(item.get("actual_visible_text") or item.get("actual_text") or "").strip()
        expected = str(item.get("expected_visible_text") or item.get("expected_text") or "").strip()
        has_copy_difference = has_copy_difference or bool((actual or expected) and actual != expected)
        if expected and actual != expected:
            visual_facts.append(f"附件显示“{actual or '空'}”，参考内容为“{expected}”")
    if has_copy_difference and result.get("problem_type") == "ui":
        focus = dict(result.get("investigation_focus")) if isinstance(result.get("investigation_focus"), Mapping) else {}
        details = [str(item) for item in focus.get("acceptance_details", []) if str(item).strip()] if isinstance(focus.get("acceptance_details"), list) else []
        focus["acceptance_details"] = list(dict.fromkeys((*details, *visual_facts)))[:12]
        focuses = [str(item) for item in focus.get("evidence_focuses", []) if str(item).strip()] if isinstance(focus.get("evidence_focuses"), list) else []
        focus["evidence_focuses"] = list(dict.fromkeys((*focuses, "可见内容的状态来源、页面消费者和已有资源或业务能力")))[:12]
        result.update(
            {
                "ui_subtype": "copy",
                "display_name": "UI 文案问题",
                "investigation_focus": focus,
                "needs_product_confirmation": isinstance(result.get("product_decision"), Mapping),
            }
        )
    return result


def select_relevant_assets(
    assets: list[dict[str, Any]],
    triage: BugTriageResult | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Keep image and log evidence while excluding unrelated documents."""
    evidence_need = triage.evidence_need if isinstance(triage, BugTriageResult) else triage.get("evidence_need")
    if evidence_need != "visual_only":
        return assets
    return [
        asset
        for asset in assets
        if str(asset.get("media_type") or "").lower().startswith(("image/", "video/", "text/"))
        or str(asset.get("media_type") or "").lower() in {"application/zip", "application/x-zip-compressed", "application/json"}
        or re.search(r"\.(?:png|jpe?g|gif|webp|heic|bmp|mp4|mov|webm|log|txt|jsonl?|zip)$", str(asset.get("name") or ""), re.IGNORECASE)
    ]
