#!/usr/bin/env python3
"""Read-only retrieval evaluation against durable historical Bug workflows.

This script never contacts ZenTao, starts Codex, or mutates a workflow.  It
builds a query from each persisted canonical fact packet, calls the configured
Tabby search endpoint, and compares the returned current-checkout paths with
source files cited by the historical final report.  The citations are a weak
reference set, not ground-truth causality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.gateway.bug_source_retrieval import build_source_retrieval  # noqa: E402

_SOURCE_REF = re.compile(r"(?P<repo>sample_mobile_repo|sample_platform_repo)/(?P<path>[A-Za-z0-9_.@+/-]+\.[A-Za-z0-9]+)(?::\d+)?")


def _historical_cases(database: Path, *, limit: int, bug_ids: set[int] | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT metadata_json
              FROM threads_meta
             WHERE assistant_id = 'bug-workflow'
               AND json_extract(metadata_json, '$.bug_workflow.analysis_report') IS NOT NULL
               AND json_extract(metadata_json, '$.bug_workflow.bug_snapshot') IS NOT NULL
             ORDER BY updated_at DESC
            """
        )
        cases: list[dict[str, Any]] = []
        seen: set[int] = set()
        for (raw,) in rows:
            metadata = json.loads(raw)
            workflow = metadata.get("bug_workflow") or {}
            bug_id = workflow.get("bug_id")
            if not isinstance(bug_id, int) or bug_id in seen:
                continue
            if bug_ids and bug_id not in bug_ids:
                continue
            report = str(workflow.get("analysis_report") or "")
            references = {(match.group("repo"), match.group("path")) for match in _SOURCE_REF.finditer(report)}
            platform = workflow.get("platform_resolution") or {}
            repository = platform.get("primary_repository")
            if repository not in {"sample_mobile_repo", "sample_platform_repo"}:
                repositories = {repo for repo, _path in references}
                repository = next(iter(repositories)) if len(repositories) == 1 else None
            reference_paths = sorted(path for repo, path in references if repo == repository)
            if not repository or not reference_paths:
                continue
            knowledge = workflow.get("investigation_knowledge_context") or {}
            runtime = {
                "log_evidence": workflow.get("runtime_log_evidence") or {},
                "runtime_pre_scan": workflow.get("runtime_pre_scan") or {},
            }
            cases.append(
                {
                    "bug_id": bug_id,
                    "repository": repository,
                    "bug_snapshot": workflow["bug_snapshot"],
                    "query_facts": knowledge.get("query_facts") or {},
                    "runtime_evidence": runtime,
                    "reference_paths": reference_paths,
                }
            )
            seen.add(bug_id)
            if len(cases) >= (len(bug_ids) if bug_ids else limit):
                break
        return cases
    finally:
        connection.close()


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("backend/.deer-flow/data/deerflow.db"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--search-path", default="/graphql")
    parser.add_argument("--api-key-file", type=Path, default=Path(".deer-flow/tabby/admin-access-token"))
    parser.add_argument("--refresh-token-file", type=Path, default=Path(".deer-flow/tabby/admin-refresh-token"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--bug-id",
        action="append",
        type=int,
        default=[],
        help="Evaluate only this persisted Bug id; repeat for multiple ids.",
    )
    parser.add_argument("--output", type=Path, default=Path(".deer-flow/audits/tabby-historical-retrieval.json"))
    parser.add_argument("--sample_mobile_repo", type=Path, default=Path("/home/example_user/projects/sample_mobile_repo"))
    parser.add_argument("--harmony-rn", type=Path, default=Path("/home/example_user/sample_platform_repo"))
    parser.add_argument("--index-root", type=Path, default=Path(".deer-flow/tabby/composite-repositories"))
    args = parser.parse_args()

    roots = {"sample_mobile_repo": args.sample_mobile_repo, "sample_platform_repo": args.sample_platform_repo}
    index_root = args.index_root.expanduser().resolve()
    urls = {name: (index_root / name).as_uri() for name in roots}
    config = SimpleNamespace(
        enabled=True,
        base_url=args.base_url,
        search_path=args.search_path,
        api_key=None,
        api_key_file=str(args.api_key_file),
        refresh_token_file=str(args.refresh_token_file),
        embedding_base_url="http://127.0.0.1:18082/v1",
        embedding_model="example-embedding-model",
        embedding_api_key=None,
        embedding_api_key_file=None,
        repository_urls=urls,
        timeout_seconds=20.0,
        max_candidates=5,
        max_anchor_queries=8,
        max_fallback_concepts=8,
        max_raw_candidates=32,
        max_query_chars=1600,
        max_snippet_chars=1800,
        max_packet_chars=10000,
        require_current_revision=True,
    )
    results: list[dict[str, Any]] = []
    selected_bug_ids = set(args.bug_id)
    raw_cases = _historical_cases(
        args.database,
        limit=max(1, args.limit) * 5,
        bug_ids=selected_bug_ids or None,
    )
    for case in raw_cases:
        case["reference_paths"] = [
            path for path in case["reference_paths"] if (roots[case["repository"]] / path).is_file()
        ]
        if not case["reference_paths"]:
            continue
        packet = await asyncio.to_thread(
            build_source_retrieval,
            repository_root=roots[case["repository"]],
            repository=case["repository"],
            bug_snapshot=case["bug_snapshot"],
            query_facts=case["query_facts"],
            runtime_evidence=case["runtime_evidence"],
            config=config,
        )
        candidate_paths = [entry["path"] for entry in packet.get("entries", [])]
        candidate_scores = [
            {"path": entry["path"], "score": entry.get("score"), "anchor": entry.get("symbol")}
            for entry in packet.get("entries", [])
        ]
        overlap = sorted(set(candidate_paths) & set(case["reference_paths"]))
        results.append(
            {
                "bug_id": case["bug_id"],
                "repository": case["repository"],
                "status": packet.get("status"),
                "query": packet.get("query"),
                "candidate_paths": candidate_paths,
                "candidate_scores": candidate_scores,
                "historical_report_paths": case["reference_paths"],
                "path_overlap": overlap,
                "top5_reference_hit": bool(overlap),
                "metrics": packet.get("metrics"),
                "rejections": packet.get("rejections"),
            }
        )
        if len(results) >= (len(selected_bug_ids) if selected_bug_ids else max(1, args.limit)):
            break
    ready = sum(item["status"] == "ready" for item in results)
    hit = sum(bool(item["top5_reference_hit"]) for item in results)
    report = {
        "schema_version": 1,
        "evaluation_kind": "historical_read_only_retrieval",
        "reference_semantics": "paths cited by historical final reports; not causal ground truth",
        "case_count": len(results),
        "ready_count": ready,
        "top5_reference_hit_count": hit,
        "top5_reference_hit_rate": round(hit / len(results), 4) if results else 0,
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("case_count", "ready_count", "top5_reference_hit_count", "top5_reference_hit_rate")}, ensure_ascii=False))
    print(args.output)
    return 0 if results and ready else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
