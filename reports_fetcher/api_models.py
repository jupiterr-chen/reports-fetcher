"""HTTP API 请求/响应模型（HTTP_API §3/§5，FastAPI/Pydantic 仅 server 安装组）。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FetchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 拒绝未知字段（HTTP_API §3）

    symbols: list[str] = Field(min_length=1, max_length=50)
    last_n: int = Field(default=4, ge=1, le=20)
    forms_by_market: dict[str, list[str]] | None = None
    refresh: bool = False


class JobAccepted(BaseModel):
    job_id: str
    status: str
    submitted_at: str
    status_url: str


class JobItemOut(BaseModel):
    report_id: str
    source_id: str
    status: str
    artifact_id: str | None = None
    error: dict | None = None


class JobSymbolOut(BaseModel):
    market: str
    symbol: str
    display_name: str | None = None
    status: str
    report_ids: list[str] = []
    items: list[JobItemOut] = []
    coverage: dict = {}
    warnings: list = []
    error: dict | None = None


class JobStatusOut(BaseModel):
    job_id: str
    status: str
    attempt: int
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    deadline: str | None = None
    progress: dict
    summary: dict
    results: list[JobSymbolOut]


class ReportListItem(BaseModel):
    report_id: str
    market: str
    symbol: str
    issuer_id: str | None = None
    source_id: str
    source_url: str | None = None
    title: str | None = None
    doc_type: str
    report_period: str | None = None
    period_source: str
    filing_date: str | None = None
    language: str | None = None
    is_amendment: bool = False
    status: str
    artifact_id: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    fetched_at: str | None = None
    warnings: list[str] = []
    download_url: str


class ReportListOut(BaseModel):
    items: list[ReportListItem]
    next_cursor: str | None = None


class ArtifactOut(BaseModel):
    artifact_id: str
    sha256: str
    bytes: int
    media_type: str
    fetched_at: str
    state: str
    is_current: bool


class ReportDetailOut(BaseModel):
    report_id: str
    market: str
    symbol: str
    source_id: str
    source_url: str | None = None
    title: str | None = None
    doc_type: str
    source_form: str | None = None
    report_period: str | None = None
    period_source: str
    filing_date: str | None = None
    language: str | None = None
    is_amendment: bool = False
    revision_of: str | None = None
    status: str
    current_artifact_id: str | None = None
    warnings: list[str] = []
    artifacts: list[ArtifactOut]


class HealthOut(BaseModel):
    status: str
    detail: str | None = None
