from typing import Literal

from pydantic import BaseModel, Field


class BugSourceRetrievalConfig(BaseModel):
    """External repository-context retrieval supplied to the Codex run.

    The service is deliberately a retriever, not another report author.  It
    must return source snippets from the selected current repository; an
    unavailable or stale service yields no candidates and Codex starts from
    the canonical facts instead.
    """

    enabled: bool = False
    provider: Literal["tabby"] = "tabby"
    base_url: str = Field(default="http://127.0.0.1:8080", description="Tabby server base URL, without a search path.")
    api_key: str | None = Field(default=None, description="Optional Tabby bearer token; $ENV references are resolved at request time.")
    api_key_file: str | None = Field(default=None, description="Optional private file containing the Tabby bearer token.")
    refresh_token_file: str | None = Field(
        default=None,
        description="Optional private Tabby user refresh token; required for authenticated hybrid-retrieval sessions.",
    )
    search_path: str = Field(
        default="/graphql",
        description="Tabby GraphQL endpoint used for repository discovery and exact-literal fallback.",
    )
    embedding_base_url: str = Field(
        default="http://127.0.0.1:18082/v1",
        description="OpenAI-compatible embedding endpoint used only when Tabby's internal hybrid retrieval is unavailable.",
    )
    embedding_model: str = "example-embedding-model"
    embedding_api_key: str | None = Field(default=None, description="Optional embedding bearer token; loopback adapter needs none.")
    embedding_api_key_file: str | None = None
    repository_urls: dict[str, str] = Field(
        default_factory=dict,
        description="Repository name to the canonical Git URL registered in Tabby; used to prevent cross-repository candidates.",
    )
    timeout_seconds: float = Field(default=12.0, gt=0, le=60)
    max_candidates: int = Field(default=5, ge=1, le=8)
    max_anchor_queries: int = Field(default=8, ge=1, le=12)
    max_fallback_concepts: int = Field(
        default=8,
        ge=1,
        le=12,
        description="Short ticket-led concepts used as a bounded second retrieval layer after exact anchors.",
    )
    max_raw_candidates: int = Field(default=32, ge=5, le=48)
    max_query_chars: int = Field(default=1_600, ge=200, le=4_000)
    max_snippet_chars: int = Field(default=1_800, ge=400, le=4_000)
    max_packet_chars: int = Field(default=10_000, ge=2_000, le=20_000)
    require_current_revision: bool = Field(
        default=True,
        description="Reject every candidate whose exact snippet is absent from the selected current checkout.",
    )


class BugAnalysisSummaryConfig(BaseModel):
    """Read-only Codex investigation and report in the selected source view."""

    codex_model_name: str = Field(default="gpt-5.6-sol", description="Local Codex model for the read-only final report.")
    codex_bin: str | None = Field(default=None, description="Optional explicit local Codex executable; bundled SDK runtime is preferred.")
    source_view_root: str = Field(
        default="~/.codex-deerflow/source-views",
        description="Gateway-host source-only views used by Codex investigations.",
    )
    investigation_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = Field(default="medium", description="Reasoning effort for a full read-only Codex investigation.")
    investigation_timeout_seconds: float = Field(
        default=3600.0,
        gt=0,
        le=7200,
        description="Whole-ticket wall-clock limit for one Codex investigation and its final report.",
    )


class BugPlatformResolutionConfig(BaseModel):
    """One bounded no-tools call that confirms only the reproduced clients."""

    model_name: str | None = Field(
        default=None,
        description="Optional model override for ticket-only platform confirmation; defaults to gpt-5.6-sol.",
    )
    max_output_tokens: int = Field(default=1_500, ge=200, le=4_000)
    thinking_enabled: bool = False
    reasoning_effort: Literal["low", "high", "max"] | None = None


class BugLogExpertConfig(BaseModel):
    """Read-only Luna selection of indexed runtime-log records."""

    model_name: str = Field(default="gpt-6-luna", description="Codex model used to select runtime-log source lines.")
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] = "medium"


class BugVisualEvidenceConfig(BaseModel):
    """Isolated no-tools Codex call used only to transcribe screenshot facts."""

    model_name: str = Field(default="gpt-6-luna", description="Codex vision model for Bug attachment fact extraction.")
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] = Field(
        default="medium",
        description="Reasoning effort for the isolated visual extraction turn.",
    )


class BugNoteRepairConfig(BaseModel):
    """Execute this run's report section four after verified note writeback."""

    enabled: bool = False
    model_name: str = "example-repair-model"
    timeout_seconds: float = Field(default=600.0, gt=0, le=3600)


class BugAnalysisConfig(BaseModel):
    """Codex investigation with optional execution of this run's report."""

    engine: Literal["codex"] = "codex"
    business_knowledge_file: str = "knowledge/bug-business-rules.json"
    source_retrieval: BugSourceRetrievalConfig = Field(default_factory=BugSourceRetrievalConfig)
    platform: BugPlatformResolutionConfig = Field(default_factory=BugPlatformResolutionConfig)
    log_expert: BugLogExpertConfig = Field(default_factory=BugLogExpertConfig)
    visual: BugVisualEvidenceConfig = Field(default_factory=BugVisualEvidenceConfig)
    summary: BugAnalysisSummaryConfig = Field(default_factory=BugAnalysisSummaryConfig)
    repair: BugNoteRepairConfig = Field(default_factory=BugNoteRepairConfig)
