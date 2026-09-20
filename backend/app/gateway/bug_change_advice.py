"""Evidence-bound advice, never write authorization or an execution plan."""

from collections.abc import Mapping, Sequence
from typing import Any

ADVICE_CONTRACT = (
    '"change_advice":[{"kind":"local_change|external_action|needs_evidence|no_change",'
    '"team":"backend|embedded|product|client|multiple|unknown",'
    '"sources":["S编号；确定修改或排除必须有source_claims原句支持；条件建议不得把待核实引用当成已证"],'
    '"problem":"现有问题或已排除原因","action":"具体修改/新增逻辑或协查动作",'
    '"basis":"为何建议这样做；与已证节点及反证的关系",'
    '"condition":"尚需核实的条件；明确时为空",'
    '"collaborators":[{"team":"client|embedded|backend|product","check":"该方具体核对内容","impact":"结果怎样改变判断"}],'
    '"decision_impact":"核实结果会改变哪个责任或改法",'
    '"acceptance":"验收点","risk":"其他端/真实状态/已有行为风险"}]'
)


def validate_change_advice(target: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Bind each item independently; uncertainty in one item cannot erase others."""
    raw_items = target.get("change_advice", [])
    if not isinstance(raw_items, list):
        return [], ["change_advice:not_list"]
    source_claims = [claim for claim in target.get("source_claims", []) if isinstance(claim, Mapping)]
    claims = {
        claim["source_id"]: claim
        for claim in source_claims
        if claim.get("source_id") and claim.get("source_ref")
    }
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw in enumerate(raw_items[:4]):
        if not isinstance(raw, Mapping):
            warnings.append(f"change_advice:not_object:{index}")
            continue
        fields = ("problem", "action", "basis", "condition", "decision_impact", "acceptance", "risk")
        if any(not isinstance(raw.get(key), str) or len(raw[key]) > 600 for key in fields):
            warnings.append(f"change_advice:invalid_fields:{index}")
            continue
        kind, team, sources = raw.get("kind"), raw.get("team"), raw.get("sources")
        if not isinstance(kind, str) or not isinstance(team, str) or kind not in {"local_change", "external_action", "needs_evidence", "no_change"} or team not in {"backend", "embedded", "product", "client", "multiple", "unknown"}:
            warnings.append(f"change_advice:invalid_kind_or_team:{index}")
            continue
        if not isinstance(sources, list) or len(sources) > 4 or any(not isinstance(source, str) for source in sources):
            warnings.append(f"change_advice:invalid_sources:{index}")
            continue
        if not all(raw[key].strip() for key in ("problem", "action", "basis")):
            warnings.append(f"change_advice:empty_action:{index}")
            continue
        valid_sources = list(dict.fromkeys(source for source in sources if source in claims))
        item = {key: raw[key].strip() for key in fields}
        collaborators = []
        for collaboration in raw.get("collaborators", [])[:3] if isinstance(raw.get("collaborators"), list) else []:
            if (
                isinstance(collaboration, Mapping)
                and isinstance(collaboration.get("team"), str)
                and collaboration["team"] in {"client", "embedded", "backend", "product"}
                and all(
                    isinstance(collaboration.get(key), str)
                    and 0 < len(collaboration[key].strip()) <= 300
                    for key in ("check", "impact")
                )
            ):
                collaborators.append({key: collaboration[key].strip() for key in ("team", "check", "impact")})
        supported = bool(sources and len(valid_sources) == len(set(sources)))
        partially_supported = bool(valid_sources)
        local_evidence_bound = bool(
            supported and target.get("consumer_proven")
            and target.get("requirement_status") in {"incorrect", "missing"}
            and partially_supported
        )
        if kind == "local_change" and not local_evidence_bound:
            # This is an analysis deliverable, not patch authorization. Keep a
            # source-bound concrete suggestion and expose uncertainty as its
            # condition instead of replacing the useful action.
            gaps = [str(gap) for gap in target.get("open_edges", []) if gap]
            item["condition"] = item["condition"] or ("；".join(gaps[:3])[:600] if gaps else "修改位置、语义或反证尚未由当前证据完整闭合；当前作为带条件建议交付。")
            item["decision_impact"] = item["decision_impact"] or "核实后决定候选改法是否成立及必要修改位置。"
            if partially_supported:
                warnings.append(f"change_advice:local_uncertainty_retained:{index}")
        open_counterevidence = any(claim.get("role") == "counterevidence" and claim.get("resolution") == "open" for claim in source_claims)
        if kind == "local_change" and open_counterevidence:
            gaps = [str(gap) for gap in target.get("open_edges", []) if gap]
            item["condition"] = item["condition"] or ("；".join(gaps[:3])[:600] if gaps else "先解释当前反证，再采用该修改意见。")
            item["decision_impact"] = item["decision_impact"] or "决定该本地建议是否成立，或是否应保留现有行为。"
            warnings.append(f"change_advice:counterevidence_condition_retained:{index}")
        external_ready = bool(
            supported and len(valid_sources) >= 2
            and target.get("runtime_trigger_status") == "observed"
            and target.get("causal_link_status") == "proven"
            and target.get("mechanism_status") == "confirmed"
            and team in {"backend", "embedded", "multiple"}
            and item["acceptance"] and item["risk"]
            and not item["condition"]
        )
        if kind == "external_action" and not external_ready:
            # A source quote alone cannot establish a remote protocol violation.
            # Keep a useful team-specific action, without manufacturing ownership.
            kind = "needs_evidence"
            item["condition"] = item["condition"] or "需外部确认接口/设备协议契约与本次实际响应；本地源码不能单独证明外部责任。"
            item["decision_impact"] = item["decision_impact"] or "区分外部违约与本地转换/覆盖问题，决定责任及配套修改。"
        if kind == "no_change" and item["condition"]:
            kind = "needs_evidence"
            item["decision_impact"] = item["decision_impact"] or "核实后判断该路径是否确实无需修改，避免漏改或重复添加已有逻辑。"
        if not supported:
            if kind == "no_change" and not partially_supported:
                warnings.append(f"change_advice:unverified_exclusion:{index}")
                continue
            if partially_supported:
                # A missing supporting binding must not erase independently
                # verified nodes or Sol's concrete local recommendation.
                missing = list(dict.fromkeys(source for source in sources if source not in claims))
                item["problem"] = ("待核实候选（不是排除或定责结论）：" + item["problem"])[:600]
                item["basis"] = (item["basis"] + "；部分引用未验真，仅保留为条件性候选，不证明修改范围或责任。")[:600]
                item["condition"] = (item["condition"] + "；" if item["condition"] else "") + "核实未绑定依据：" + "、".join(missing)
                item["condition"] = item["condition"][:600]
                item["decision_impact"] = item["decision_impact"] or "决定候选路径是否成立、哪些文件确需修改以及责任边界。"
                if kind in {"external_action", "no_change"}:
                    kind = "needs_evidence"
                warnings.append(f"change_advice:partial_support_retained:{index}")
            else:
                kind = "needs_evidence"
                team = "unknown"
                collaborators = []
                item["problem"] = "候选问题尚未绑定已验真证据。"
                item["action"] = "先核实以下开放边，再决定修改范围与协查对象。"
                item["basis"] = "原意见缺少已验真依据，不作为确定建议。"
                gaps = [str(gap) for gap in target.get("open_edges", []) if gap]
                item["condition"] = "；".join(gaps[:3])[:600] or "核实当前消费者与最近失败边之间的实际传递、响应及状态更新。"
                item["decision_impact"] = "区分本地实现错误、外部响应异常和未实现需求；据结果选择修改或协查。"
            warnings.append(f"change_advice:sources_unverified:{index}")
        if kind == "needs_evidence" and not (item["condition"] and item["decision_impact"]):
            warnings.append(f"change_advice:gap_unspecified:{index}")
            continue
        result.append({**item, "kind": kind, "team": "unknown" if kind == "local_change" else team,
                       "collaborators": collaborators if kind == "needs_evidence" else [],
                       "evidence": [claims[source]["source_ref"] for source in valid_sources],
                       "implementation_status": "not_implemented_not_verified"})
    return result, warnings


def render_change_advice(targets: Sequence[Mapping[str, Any]]) -> str:
    teams = {"backend": "后端", "embedded": "嵌入式", "product": "产品", "client": "客户端", "multiple": "相关多方", "unknown": "协查团队暂未确定"}
    lines = ["以下为建议，未实施、未验证；不构成自动修改授权。"]
    for target in targets:
        lines.append(f"\n{target.get('target_id', 'target')}：{target.get('target_behavior', '')}")
        advice = target.get("change_advice", [])
        if not advice:
            lines.append("尚无证据闭合的具体改法；不得把已读文件列表当成修改清单。")
            for gap in target.get("open_edges", [])[:4]:
                lines.append(f"- 待补证据：{gap}")
        for item in advice:
            title = "具体修改意见" if item["kind"] == "local_change" else "不建议修改该路径" if item["kind"] == "no_change" else "外部处理建议" if item["kind"] == "external_action" else "候选方向 / 建议协查（责任未确认）"
            lines.extend((f"- {title}：{item['problem']}", f"  建议动作：{item['action']}", f"  依据：{item['basis']}"))
            if item["evidence"]:
                lines.append(f"  已验真位置：{'；'.join(item['evidence'])}")
            if item["kind"] == "needs_evidence":
                lines.extend((f"  协查对象：{teams[item['team']]}", f"  待确认：{item['condition']}", f"  判断影响：{item['decision_impact']}"))
                for collaboration in item.get("collaborators", []):
                    lines.append(f"  {teams[collaboration['team']]}核对：{collaboration['check']}；判断影响：{collaboration['impact']}")
            elif item["kind"] == "local_change" and item["condition"]:
                lines.extend((f"  适用条件：{item['condition']}", f"  判断影响：{item['decision_impact']}"))
            elif item["kind"] == "external_action":
                lines.append(f"  建议指派：{teams[item['team']]}")
            if item["acceptance"]:
                lines.append(f"  验收：{item['acceptance']}")
            if item["risk"]:
                lines.append(f"  风险：{item['risk']}")
    return "\n".join(lines)
