"""Small private, scope-aware business recall; never executable navigation."""

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deerflow.config.paths import get_paths

_MAX_RETRIEVAL_ALIASES_PER_RULE = 8
_MAX_RETRIEVAL_ALIASES_PER_PACKET = 12


def _retrieval_aliases(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_RETRIEVAL_ALIASES_PER_RULE:
        return []
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in value:
        alias = str(raw).strip() if isinstance(raw, str) else ""
        folded = alias.casefold()
        if not alias or len(alias) > 80 or "\n" in alias or "\r" in alias or folded in seen:
            continue
        seen.add(folded)
        aliases.append(alias)
    return aliases


def matched_retrieval_aliases(packet: Mapping[str, Any]) -> list[str]:
    """Return bounded aliases from rules whose declared scope actually matched."""
    aliases: list[str] = []
    seen: set[str] = set()
    rules = packet.get("rules")
    if not isinstance(rules, list):
        return aliases
    for rule in rules:
        if (
            not isinstance(rule, Mapping)
            or rule.get("applicability") != "matched"
            or rule.get("basis") != "explicit"
            or rule.get("review_status") != "primary_verified"
        ):
            continue
        for alias in _retrieval_aliases(rule.get("retrieval_aliases")):
            folded = alias.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            aliases.append(alias)
            if len(aliases) >= _MAX_RETRIEVAL_ALIASES_PER_PACKET:
                return aliases
    return aliases


def recall_business_rules(filename: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Fail open, retaining provenance and uncertainty rather than guessing scope."""
    packet: dict[str, Any] = {"rules": [], "diagnostics": [], "semantics": "business_context_only; not runtime or ownership proof"}
    if not filename:
        return packet
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = get_paths().base_dir / path
    try:
        if path.stat().st_size > 262_144:
            raise ValueError("knowledge_too_large")
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, Mapping) or data.get("schema_version") != 1 or not isinstance(data.get("rules"), list):
            raise ValueError("invalid_knowledge_schema")
    except FileNotFoundError:
        packet["diagnostics"] = ["private_knowledge_not_installed"]
        return packet
    except (OSError, ValueError, UnicodeError):
        packet["diagnostics"] = ["private_knowledge_unavailable_or_invalid"]
        return packet
    packet["content_hash"] = hashlib.sha256(raw).hexdigest()
    # Only textual ticket facts select scope, never image-derived platform or
    # identifiers embedded in inferred navigation/map output.
    text = "\n".join(str(snapshot.get(key) or "") for key in ("title", "description", "steps", "actual", "expected", "product"))
    ranked: list[tuple[int, dict[str, Any]]] = []
    seen: set[str] = set()
    known_models: set[str] = set()
    for candidate in data["rules"][:128]:
        models = candidate.get("models", []) if isinstance(candidate, Mapping) else []
        if isinstance(models, list):
            known_models.update(model for model in models if isinstance(model, str) and re.search(r"(?<![A-Za-z0-9])" + re.escape(model) + r"(?![A-Za-z0-9])", text, re.IGNORECASE))
    for rule in data["rules"][:128]:
        if not isinstance(rule, Mapping):
            continue
        required = ("id", "statement", "source_url", "source_section", "basis", "review_status")
        if any(not isinstance(rule.get(key), str) or not rule[key].strip() or len(rule[key]) > 700 for key in required):
            continue
        if rule["id"] in seen or rule["basis"] not in {"explicit", "inferred"} or rule["review_status"] not in {"primary_verified", "secondary_only"}:
            continue
        if not rule["source_url"].startswith("https://") or rule.get("deprecated"):
            continue
        lists = ("match_terms", "models", "versions", "runtime_signals")
        if any(not isinstance(rule.get(key, []), list) or len(rule.get(key, [])) > 16 or any(not isinstance(value, str) or not value or len(value) > 160 for value in rule.get(key, [])) for key in lists):
            continue
        hits = [term for term in rule.get("match_terms", []) if term.casefold() in text.casefold()]
        if not hits:
            continue
        if rule.get("models") and known_models and not known_models.intersection(rule["models"]):
            continue
        scopes = {key: any(re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", text, re.IGNORECASE) for value in rule.get(key, [])) for key in ("models", "versions")}
        applicable = all(not rule.get(key) or scopes[key] for key in scopes)
        certainty = "requirement_reference" if applicable and rule["basis"] == "explicit" and rule["review_status"] == "primary_verified" and not rule.get("conflict") else "conditional_reference"
        item = {key: rule[key] for key in required}
        item.update({key: list(rule.get(key, [])) for key in ("models", "versions", "runtime_signals")})
        aliases = _retrieval_aliases(rule.get("retrieval_aliases"))
        if aliases:
            item["retrieval_aliases"] = aliases
        item.update({"applicability": "matched" if applicable else "scope_unconfirmed", "certainty": certainty,
                     "limits": "型号/版本不符不得套用；推断、未核对原文及冲突均需核实；协议规则不能证明本次实际责任。",
                     "conflict": str(rule.get("conflict") or "")[:400]})
        ranked.append((len(hits) + (4 if applicable else 0), item))
        seen.add(rule["id"])
    packet["rules"] = [item for _, item in sorted(ranked, key=lambda value: -value[0])[:4]]
    return packet
