"""Conditional ZenTao attachment evidence side-path for Bug Workbench."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import re
import shutil
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from imageio_ffmpeg import get_ffmpeg_exe
from zentao_mcp.client import ZentaoError

from deerflow.config import get_app_config
from deerflow.uploads.manager import claim_unique_filename, normalize_filename

MAX_EVIDENCE_FILES = 10
MAX_EVIDENCE_FILE_BYTES = 50 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_VISUAL_FILES = 6
MAX_VISUAL_FILE_BYTES = 8 * 1024 * 1024
MAX_VISUAL_EVIDENCE_ITEMS = 24
MAX_VISUAL_COMPARISONS = 12
VISUAL_EVIDENCE_SCHEMA_VERSION = 2
MAX_ARCHIVE_MEMBERS = 20
VISUAL_FAST_TIMEOUT_SECONDS = 180.0
VISUAL_THINKING_TIMEOUT_SECONDS = 300.0

_SENSITIVE_TEXT_PATTERN = re.compile(
    r"""(?imx)
    (
        ["']?
        \b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|cookie|password|passwd|secret)\b
        ["']?\s*[:=]\s*["']?
        (?:(?:bearer|basic)\s+)?
    )
    ([^\s,;"'&]+)
    """,
)
_EXPECTED_TIME_PLACEHOLDER = re.compile(r"^#time#\s*", re.IGNORECASE)

ProgressCallback = Callable[[list[dict[str, Any]]], Awaitable[None]]
VisualAnalyzer = Callable[[list[Path]], Awaitable[str | dict[str, Any] | list[dict[str, Any]]]]
logger = logging.getLogger(__name__)


def _public_asset(asset: dict[str, Any], *, status: str, name: str | None = None, path: str | None = None, error: str | None = None) -> dict[str, Any]:
    result = {
        "id": str(asset.get("id") or ""),
        "name": name or str(asset.get("name") or "附件"),
        "source": str(asset.get("source") or "attachment"),
        "media_type": str(asset.get("media_type") or "application/octet-stream"),
        "size": asset.get("size") if isinstance(asset.get("size"), int) else None,
        "status": status,
    }
    if path:
        result["path"] = path
    if asset.get("ticket_section") in {"actual", "expected"}:
        result["ticket_section"] = asset["ticket_section"]
    if error:
        result["error"] = error
    return result


def _is_text(media_type: str, name: str) -> bool:
    return media_type.startswith("text/") or Path(name).suffix.lower() in {".csv", ".json", ".log", ".md", ".txt", ".xml", ".yaml", ".yml"}


def _redact_text(value: str) -> str:
    """Keep log evidence useful without forwarding common credentials."""
    return _SENSITIVE_TEXT_PATTERN.sub(r"\1[REDACTED]", value)


def _comparison_anchor(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _visual_fact(item: dict[str, Any], *keys: str) -> str:
    return next((str(item.get(key) or "").strip() for key in keys if str(item.get(key) or "").strip()), "")


def _derive_visual_comparisons(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add conservative actual/expected links without replacing either source fact."""
    comparisons: list[dict[str, Any]] = []

    def add(actual_ids: list[str], expected_ids: list[str], *, anchor: str, basis: str) -> None:
        if len(comparisons) >= MAX_VISUAL_COMPARISONS:
            return
        identity = (tuple(actual_ids), tuple(expected_ids))
        if any((tuple(item["actual_item_ids"]), tuple(item["expected_item_ids"])) == identity for item in comparisons):
            return
        comparisons.append(
            {
                "comparison_id": f"comparison_{len(comparisons) + 1}",
                "actual_item_ids": actual_ids,
                "expected_item_ids": expected_ids,
                "anchor": anchor,
                "basis": basis,
            }
        )

    actual_items: list[dict[str, Any]] = []
    expected_items: list[dict[str, Any]] = []
    for item in items:
        actual_fact = _visual_fact(item, "actual_visible_text", "actual_visual")
        expected_fact = _visual_fact(item, "expected_visible_text", "expected_visual")
        if actual_fact:
            actual_items.append(item)
        if expected_fact:
            expected_items.append(item)
        if actual_fact and expected_fact:
            anchor = _visual_fact(item, "event_id", "resource_key", "region_or_control", "event_or_action", "page")
            add([item["visual_item_id"]], [item["visual_item_id"]], anchor=anchor, basis="same_item_actual_expected_facts")

    # Separate facts are linked only by an exact, unique business anchor. A
    # fuzzy visual match could silently turn unrelated screenshots into a
    # requirement and is therefore left for Codex to evaluate.
    anchor_fields = ("event_id", "resource_key", "region_or_control", "event_or_action")
    for actual in actual_items:
        if _visual_fact(actual, "expected_visible_text", "expected_visual"):
            continue
        for field in anchor_fields:
            anchor = _comparison_anchor(actual.get(field))
            if not anchor:
                continue
            matches = [expected for expected in expected_items if expected["visual_item_id"] != actual["visual_item_id"] and _comparison_anchor(expected.get(field)) == anchor]
            if len(matches) == 1:
                add(
                    [actual["visual_item_id"]],
                    [matches[0]["visual_item_id"]],
                    anchor=str(actual.get(field) or ""),
                    basis=f"exact_{field}",
                )
                break
    return comparisons


def _minimal_visual_evidence(value: Any, *, allowed_asset_names: set[str] | None = None) -> dict[str, Any]:
    """Restore rich visual facts while retaining the four legacy aliases."""
    if isinstance(value, dict):
        raw_items = value.get("items")
        items = raw_items if isinstance(raw_items, list) else [value]
    else:
        items = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []

    def text(item: dict[str, Any], *keys: str, limit: int = 600) -> str:
        for key in keys:
            candidate = item.get(key)
            if candidate not in (None, ""):
                return re.sub(r"\s+", " ", str(candidate)).strip()[:limit]
        return ""

    for item in (items if isinstance(items, list) else [])[:MAX_VISUAL_EVIDENCE_ITEMS]:
        if not isinstance(item, dict):
            continue
        asset_role = text(item, "asset_role", limit=60).lower()
        client_evidence = text(item, "client_evidence", limit=240)
        raw_clients = item.get("observed_clients")
        clients = [str(candidate).lower() for candidate in raw_clients if str(candidate).lower() in {"android", "ios", "harmony"}] if isinstance(raw_clients, list) else []
        explicit_client = str(item.get("client") or "").lower()
        if explicit_client in {"android", "ios", "harmony"} and explicit_client not in clients:
            clients.insert(0, explicit_client)
        if not clients:
            evidence_clients = [
                client
                for client, pattern in (
                    ("android", r"android"),
                    ("ios", r"ios"),
                    ("harmony", r"harmony|鸿蒙"),
                )
                if re.search(pattern, client_evidence, re.IGNORECASE)
            ]
            if len(evidence_clients) == 1:
                clients = evidence_clients
        # Tables, desktop document viewers and expected/reference material do
        # not prove which mobile client reproduced the Bug. Older analyzer
        # payloads have no asset_role, so preserve their clients for checkpoint
        # compatibility while enforcing the role contract for all new runs.
        if asset_role in {"reference_table", "expected_reference", "supplementary_material", "desktop_document"}:
            clients = []
        client = clients[0] if clients else "unknown"
        page = text(item, "page", "page_or_region")
        region = text(item, "region_or_control", "page_or_region")
        actual_text = text(item, "actual_visible_text", "actual_text")
        expected_text = _EXPECTED_TIME_PLACEHOLDER.sub(
            "",
            text(item, "expected_visible_text", "expected_text"),
        )
        confidence = text(item, "confidence", limit=20).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "unknown"
        evidence_refs: list[dict[str, str]] = []
        raw_refs = item.get("evidence_refs")
        for reference in raw_refs if isinstance(raw_refs, list) else []:
            if not isinstance(reference, dict):
                continue
            normalized_ref = {key: text(reference, key, limit=160) for key in ("asset", "role", "annotation") if text(reference, key, limit=160)}
            if allowed_asset_names is not None and normalized_ref.get("asset") not in allowed_asset_names:
                continue
            if normalized_ref:
                evidence_refs.append(normalized_ref)
            if len(evidence_refs) >= MAX_VISUAL_FILES:
                break
        evidence_refs = [dict(values) for _identity, values in sorted({(reference.get("asset", ""), reference.get("role", ""), reference.get("annotation", "")): reference for reference in evidence_refs}.items())]
        result.append(
            {
                "visual_item_id": f"visual_{len(result) + 1}",
                "asset_role": asset_role or "unknown",
                "observed_clients": clients,
                "client_evidence": client_evidence,
                "product_variant": text(item, "product_variant"),
                "user_path": text(item, "user_path"),
                "page": page,
                "region_or_control": region,
                "page_state": text(item, "page_state"),
                "actual_visible_text": actual_text,
                "expected_visible_text": expected_text,
                "highlighted_content": text(item, "highlighted_content"),
                "actual_visual": text(item, "actual_visual"),
                "expected_visual": text(item, "expected_visual"),
                "visual_difference": text(item, "visual_difference", "mismatch_summary"),
                "element_type": text(item, "element_type", limit=80),
                "mismatch_summary": text(item, "mismatch_summary", "visual_difference"),
                "event_or_action": text(item, "event_or_action", "event_type", "action", limit=160),
                "event_id": text(item, "event_id", limit=160),
                "resource_key": text(item, "resource_key", "text_key", "copy_key", limit=160),
                "confidence": confidence,
                "evidence_refs": evidence_refs,
                # Compatibility aliases consumed by the existing map/resource tools.
                "client": client,
                "page_or_region": region or page,
                "actual_text": actual_text,
                "expected_text": expected_text,
            }
        )
    deduplicated: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for item in result:
        identity_payload = {key: value for key, value in item.items() if key not in {"visual_item_id", "confidence"}}
        identity = json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if identity in seen_items:
            continue
        seen_items.add(identity)
        item["visual_item_id"] = f"visual_{len(deduplicated) + 1}"
        deduplicated.append(item)
    return {
        "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
        "items": deduplicated,
        "comparisons": _derive_visual_comparisons(deduplicated),
    }


def _archive_index(payload: bytes, archive_name: str) -> list[dict[str, Any]]:
    """Return metadata only: archive contents are never first-round evidence."""
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        return []
    members_index: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()][:MAX_ARCHIVE_MEMBERS]
            for member in members:
                if member.flag_bits & 0x1 or member.file_size <= 0:
                    continue
                member_name = member.filename.replace("\\", "/").lstrip("/")
                members_index.append({"archive": archive_name, "name": member_name, "media_type": _media_type_for_name(member_name), "size": member.file_size})
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return []
    return members_index


def _media_type_for_name(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _expected_signature_type_for_name(name: str) -> str | None:
    """Limit mismatch checks to formats whose container signature is definitive."""
    return {
        ".zip": "application/zip",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
    }.get(Path(name).suffix.lower())


def _sniff_media_type(payload: bytes) -> str | None:
    """Return a type only when the file signature is strong enough to trust."""
    if zipfile.is_zipfile(io.BytesIO(payload)):
        return "application/zip"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        return "video/quicktime" if payload[8:12] == b"qt  " else "video/mp4"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _media_type_label(media_type: str) -> str:
    labels = {
        "application/zip": "ZIP",
        "video/mp4": "MP4",
        "video/quicktime": "MOV",
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/gif": "GIF",
        "application/pdf": "PDF",
    }
    return labels.get(media_type, media_type)


def _signature_types_compatible(filename_type: str, signature_type: str) -> bool:
    """Treat MP4 and QuickTime brands as one decodable video-container family."""
    if filename_type == signature_type:
        return True
    iso_video_family = {"video/mp4", "video/quicktime"}
    return {filename_type, signature_type}.issubset(iso_video_family)


def _ffmpeg_executable() -> str:
    """Resolve the host decoder first and the pinned bundled decoder second."""
    return shutil.which("ffmpeg") or get_ffmpeg_exe()


async def _extract_video_frames(video: Path) -> list[Path]:
    """Extract a bounded visual sample with system or bundled ffmpeg."""
    pattern = video.with_name(f".{video.stem}-frame-%02d.jpg")
    executable = _ffmpeg_executable()
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            "fps=1/10,scale='min(1280,iw)':-2",
            "-frames:v",
            "6",
            "-y",
            str(pattern),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() != 0:
            return []
    except (FileNotFoundError, OSError):
        return []
    return sorted(video.parent.glob(f".{video.stem}-frame-*.jpg"))[:6]


async def _default_visual_analyzer(paths: list[Path], *, ticket_sections: Mapping[str, str] | None = None, thinking_enabled: bool = False) -> str:
    """Extract screenshot facts in one fresh, tool-free Codex Luna turn."""
    from openai_codex import ApprovalMode, AsyncCodex, LocalImageInput, Sandbox, TextInput
    from openai_codex.generated.v2_all import ReasoningEffort

    from app.gateway.bug_codex_summary import _codex_config

    accepted: list[Path] = []
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if 0 < size <= MAX_VISUAL_FILE_BYTES:
            accepted.append(path)
        if len(accepted) >= MAX_VISUAL_FILES:
            break
    if not accepted:
        return ""

    prompt = (
        "逐张、逐面板提取这些禅道 Bug 图片里的可见事实。优先读取红框、箭头、高亮、下划线和批注，"
        "但不得漏掉同一长图或拼图中的其他独立异常；每个独立文案、控件状态或布局事实单独一个 item。"
        "正文已标明预期/实际时必须服从该标记；未标明时才判断 asset_role。不要判断产品、客户端、"
        "Native/RN、源码归属、根因或修复方案，不可猜测不可见内容。敏感值写 [REDACTED]。"
        '只输出 JSON，不加 Markdown：{"items":[{"asset_role":"actual_app_screenshot|expected_reference|'
        'reference_table|desktop_document|supplementary_material","page":"","region_or_control":"",'
        '"page_state":"","actual_visible_text":"","expected_visible_text":"",'
        '"highlighted_content":"","actual_visual":"","expected_visual":"",'
        '"visual_difference":"","element_type":"","event_or_action":"",'
        '"confidence":"high|medium|low","evidence_refs":[{"asset":"真实文件名",'
        '"role":"与asset_role相同","annotation":"red_box|arrow|highlight|underline|note|none"}]}]}。'
        "没有预期材料时保留全部实际事实，不得补造预期；#time# 仅为预期时间占位符，转写时删除。"
    )
    items: list[Any] = [TextInput(prompt)]
    for path in accepted:
        section = (ticket_sections or {}).get(path.name)
        section_label = "预期结果" if section == "expected" else "实际结果" if section == "actual" else "未标明"
        items.extend((TextInput(f"文件：{path.name}；禅道正文段落：{section_label}"), LocalImageInput(str(path))))

    app_config = get_app_config()
    visual_config = app_config.bug_analysis.visual
    base_config = _codex_config(app_config.bug_analysis.summary.codex_bin)
    codex_config = replace(
        base_config,
        cwd=str(accepted[0].parent),
        config_overrides=base_config.config_overrides
        + (
            "features.shell_tool=false",
            "features.unified_exec=false",
            "features.apply_patch_freeform=false",
            "features.multi_agent=false",
            "features.apps=false",
            "features.plugins=false",
            "features.remote_plugin=false",
            "features.view_image=false",
            "features.image_generation=false",
            'web_search="disabled"',
            "project_doc_max_bytes=0",
        ),
    )
    async with AsyncCodex(codex_config) as codex:
        thread = await codex.thread_start(
            ephemeral=True,
            service_name="deerflow_bug_visual_evidence",
            model=visual_config.model_name,
            cwd=str(accepted[0].parent),
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            developer_instructions=(
                "You are a read-only visual fact extractor. Use only the supplied images and text. Do not use tools, inspect the filesystem, infer source ownership, or propose a fix. Return the requested JSON as the entire final response."
            ),
        )
        result = await thread.run(items, effort=ReasoningEffort(visual_config.reasoning_effort))
    return (result.final_response or "").strip()


def _apply_ticket_sections(evidence: dict[str, Any], ticket_sections: Mapping[str, str]) -> dict[str, Any]:
    """Keep explicit Bug-body image roles authoritative over model guesses."""
    for item in evidence["items"]:
        sections: set[str] = set()
        for reference in item["evidence_refs"]:
            section = ticket_sections.get(reference.get("asset", ""))
            if section not in {"actual", "expected"}:
                continue
            sections.add(section)
            reference["role"] = "expected_reference" if section == "expected" else "actual_app_screenshot"
        if len(sections) != 1 or len(item["evidence_refs"]) != 1:
            continue
        section = sections.pop()
        item["asset_role"] = "expected_reference" if section == "expected" else "actual_app_screenshot"
        target, other = ("expected", "actual") if section == "expected" else ("actual", "expected")
        for suffix in ("visible_text", "visual"):
            target_key = f"{target}_{suffix}"
            other_key = f"{other}_{suffix}"
            item[target_key] = item[target_key] or item[other_key]
            item[other_key] = ""
        item["actual_text"] = item["actual_visible_text"]
        item["expected_text"] = item["expected_visible_text"]
        item["visual_difference"] = ""
        item["mismatch_summary"] = ""
        if section == "expected":
            item["observed_clients"] = []
            item["client"] = "unknown"
            item["client_evidence"] = ""
    evidence["comparisons"] = _derive_visual_comparisons(evidence["items"])
    return evidence


def _normalize_visual_analysis(
    value: Any,
    *,
    allowed_asset_names: set[str] | None = None,
    ticket_sections: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return normalized evidence and whether the provider response was valid JSON-like data."""
    parsed = value
    valid = isinstance(parsed, (dict, list))
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
            valid = isinstance(parsed, (dict, list))
        except json.JSONDecodeError:
            parsed = {}
            valid = False
    evidence = _minimal_visual_evidence(parsed, allowed_asset_names=allowed_asset_names)
    if ticket_sections:
        evidence = _apply_ticket_sections(evidence, ticket_sections)
    return evidence, valid


def _visual_quality_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    items = evidence.get("items") if isinstance(evidence.get("items"), list) else []
    meaningful = [
        item
        for item in items
        if isinstance(item, dict)
        and _visual_fact(
            item,
            "actual_visible_text",
            "expected_visible_text",
            "highlighted_content",
            "actual_visual",
            "expected_visual",
            "visual_difference",
            "mismatch_summary",
        )
    ]
    referenced = [item for item in meaningful if item.get("evidence_refs")]
    comparisons = evidence.get("comparisons") if isinstance(evidence.get("comparisons"), list) else []
    return {
        "meaningful_items": len(meaningful),
        "referenced_items": len(referenced),
        "comparison_count": len(comparisons),
        "weak_items": max(0, len(items) - len(meaningful)),
        "usable": bool(meaningful and referenced),
    }


def _visual_input_provenance(paths: list[Path]) -> list[dict[str, Any]]:
    provenance: list[dict[str, Any]] = []
    for path in paths[:MAX_VISUAL_FILES]:
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        provenance.append({"asset": path.name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    return provenance


async def collect_bug_attachment_evidence(
    client: Any,
    bug_id: int,
    assets: list[dict[str, Any]],
    uploads_dir: Path,
    *,
    on_progress: ProgressCallback | None = None,
    visual_analyzer: VisualAnalyzer | None = None,
) -> dict[str, Any] | None:
    """Download and summarize Bug media without participating when none exists."""
    if not assets:
        return None
    uploads_dir.mkdir(parents=True, exist_ok=True)
    candidates = [item for item in assets if isinstance(item, dict)]
    selected = candidates[:MAX_EVIDENCE_FILES]
    states = [_public_asset(item, status="queued") for item in selected]
    states.extend(_public_asset(item, status="failed", error="超过附件数量限制") for item in candidates[MAX_EVIDENCE_FILES:])
    if on_progress:
        await on_progress([dict(item) for item in states])

    seen: set[str] = set()
    total_bytes = 0
    attachment_index: list[dict[str, Any]] = []
    visual_paths: list[Path] = []
    visual_ticket_sections: dict[str, str] = {}
    visual_asset_indexes: set[int] = set()

    for index, asset in enumerate(selected):
        states[index] = _public_asset(asset, status="downloading")
        if on_progress:
            await on_progress([dict(item) for item in states])
        declared_size = asset.get("size")
        if isinstance(declared_size, int) and (declared_size > MAX_EVIDENCE_FILE_BYTES or total_bytes + declared_size > MAX_EVIDENCE_TOTAL_BYTES):
            states[index] = _public_asset(asset, status="failed", error="文件超过下载限制")
            if on_progress:
                await on_progress([dict(item) for item in states])
            continue
        try:
            payload, response_type = await client.download_evidence_asset(bug_id, asset, max_bytes=min(MAX_EVIDENCE_FILE_BYTES, MAX_EVIDENCE_TOTAL_BYTES - total_bytes))
            if total_bytes + len(payload) > MAX_EVIDENCE_TOTAL_BYTES:
                raise ValueError("total limit")
            safe_name = claim_unique_filename(normalize_filename(str(asset.get("name") or f"attachment-{index + 1}")), seen)
            target = uploads_dir / safe_name
            target.write_bytes(payload)
            total_bytes += len(payload)
            declared_type = str(asset.get("media_type") or "application/octet-stream")
            signature_type = _sniff_media_type(payload)
            media_type = signature_type or (response_type if response_type and response_type != "application/octet-stream" else declared_type)
            public_path = f"/mnt/user-data/uploads/{safe_name}"
            normalized_asset = {**asset, "media_type": media_type, "size": len(payload)}
            states[index] = _public_asset(normalized_asset, status="downloaded", name=safe_name, path=public_path)
            filename_type = _expected_signature_type_for_name(safe_name)
            if signature_type and filename_type and not _signature_types_compatible(filename_type, signature_type):
                states[index] = _public_asset(
                    normalized_asset,
                    status="type_mismatch",
                    name=safe_name,
                    path=public_path,
                    error=(f"文件名声明为 {_media_type_label(filename_type)}，实际内容为 {_media_type_label(signature_type)}，疑似附件上传或关联错误"),
                )
                if on_progress:
                    await on_progress([dict(item) for item in states])
                continue
            if _is_text(media_type, safe_name):
                attachment_index.append({"name": safe_name, "path": public_path, "media_type": media_type, "kind": "text_or_log", "status": "available", "size": len(payload)})
                states[index]["status"] = "indexed"
            elif media_type in {"application/zip", "application/x-zip-compressed"} or Path(safe_name).suffix.lower() == ".zip":
                attachment_index.extend(_archive_index(payload, safe_name))
                states[index]["status"] = "indexed"
            elif media_type.startswith("image/"):
                visual_paths.append(target)
                if asset.get("ticket_section") in {"actual", "expected"}:
                    visual_ticket_sections[target.name] = asset["ticket_section"]
                visual_asset_indexes.add(index)
            elif media_type.startswith("video/"):
                frames = await _extract_video_frames(target)
                visual_paths.extend(frames)
                if frames:
                    visual_asset_indexes.add(index)
        except (OSError, ValueError, ZentaoError):
            states[index] = _public_asset(asset, status="failed", error="下载失败")
        if on_progress:
            await on_progress([dict(item) for item in states])

    visual_evidence: Any = {"items": [], "status": "not_run"}
    if visual_paths:
        visual_error = ""
        normalized_evidence: dict[str, Any] = {"items": []}
        normalized_valid = False
        quality_summary: dict[str, Any] = {}
        attempt_count = 0
        selected_thinking_enabled = False
        response_sha256 = ""
        allowed_asset_names = {path.name for path in visual_paths}
        attempts = (
            (False, VISUAL_FAST_TIMEOUT_SECONDS, "visual_fast_processing"),
            (True, VISUAL_THINKING_TIMEOUT_SECONDS, "visual_thinking_retry"),
        )
        for thinking_enabled, timeout_seconds, progress_status in attempts:
            attempt_count += 1
            for index in visual_asset_indexes:
                states[index]["status"] = progress_status
                states[index].pop("error", None)
            if on_progress:
                await on_progress([dict(item) for item in states])
            try:
                if visual_analyzer is None:
                    raw_evidence = await asyncio.wait_for(
                        _default_visual_analyzer(visual_paths, ticket_sections=visual_ticket_sections, thinking_enabled=thinking_enabled),
                        timeout=timeout_seconds,
                    )
                else:
                    raw_evidence = await asyncio.wait_for(visual_analyzer(visual_paths), timeout=timeout_seconds)
                normalized_evidence, normalized_valid = _normalize_visual_analysis(
                    raw_evidence,
                    allowed_asset_names=allowed_asset_names,
                    ticket_sections=visual_ticket_sections,
                )
                quality_summary = _visual_quality_summary(normalized_evidence)
                response_sha256 = hashlib.sha256((raw_evidence if isinstance(raw_evidence, str) else json.dumps(raw_evidence, ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest()
                selected_thinking_enabled = thinking_enabled
                if normalized_valid and normalized_evidence.get("items") and quality_summary.get("usable"):
                    break
                visual_error = "视觉识别未返回带真实附件引用的有效证据"
            except TimeoutError:
                visual_error = "视觉识别超时"
                logger.warning("Bug attachment visual analysis timed out: thinking_enabled=%s", thinking_enabled)
            except Exception:
                visual_error = "视觉分析失败"
                logger.exception("Bug attachment visual analysis failed: thinking_enabled=%s", thinking_enabled)
        visual_evidence = normalized_evidence
        visual_config = get_app_config().bug_analysis.visual
        visual_evidence["extractor"] = {
            "model": f"codex/{visual_config.model_name}",
            "reasoning_effort": visual_config.reasoning_effort,
            "schema_version": VISUAL_EVIDENCE_SCHEMA_VERSION,
            "attempt_count": attempt_count,
            "thinking_enabled": selected_thinking_enabled,
            "response_sha256": response_sha256,
            "input_assets": _visual_input_provenance(visual_paths),
        }
        visual_evidence["quality"] = quality_summary
        visual_items = visual_evidence.get("items") if isinstance(visual_evidence, dict) else []
        if not normalized_valid:
            visual_evidence["status"] = "failed"
            for index in visual_asset_indexes:
                states[index]["status"] = "visual_analysis_failed"
                states[index]["error"] = visual_error or "视觉分析失败"
        elif visual_items:
            visual_evidence["status"] = "processed"
            for index in visual_asset_indexes:
                states[index]["status"] = "processed"
        else:
            visual_evidence["status"] = "no_evidence"
            for index in visual_asset_indexes:
                states[index]["status"] = "visual_no_evidence"
        if on_progress:
            await on_progress([dict(item) for item in states])

    return {
        "assets": states,
        "attachment_index": attachment_index,
        "visual_evidence": visual_evidence,
    }
