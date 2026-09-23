#!/usr/bin/env python3
"""reports-fetcher local mock HTTP service (integration-kit).

Standard library only: no pip install, no dependency on the main project, and
no outbound network requests. It implements the public HTTP contract of
reports-fetcher v1.0.3 closely enough to develop and test an HTTP client
completely offline, plus a small mock-only control surface under ``/__mock/*``
that MUST never be sent to production.

Runtime state is a thread-safe in-memory dict. Nothing is written to disk
(except that the example client may save downloaded fixture bytes).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent
FIXTURE_DIR = HERE / "fixtures"
OPENAPI_PATH = KIT_ROOT / "openapi.json"

REQUEST_BODY_LIMIT = 64 * 1024
_IDEMPOTENCY_RE = re.compile(r"^[!-~]{1,128}$")
_US_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

TERMINAL_STATUSES = ("succeeded", "partial", "failed")

# Supported forms = what an explicit forms_by_market entry may contain.
SUPPORTED_FORMS = {
    "CN": {"Q1", "H1", "Q3", "FY"},
    "HK": {"ANNUAL", "INTERIM", "QTR-HK"},
    "US": {"10-Q", "10-K", "20-F"},
}
# Effective defaults, matching config.fetch.default_forms (v1.0.3):
# HK default now includes the voluntary quarterly announcements (QTR-HK).
# An issuer without quarterly disclosures simply skips them (no error).
DEFAULT_FORMS = {
    "CN": sorted(["Q1", "H1", "Q3", "FY"]),
    "HK": sorted(["ANNUAL", "INTERIM", "QTR-HK"]),
    "US": sorted(["10-Q", "10-K", "20-F"]),
}


def _hk_has_quarterly(symbol: str) -> bool:
    """Deterministic mock issuer diversity: an HK symbol whose last digit is
    EVEN publishes voluntary quarterly results (e.g. 0700.HK -> yes, echoing
    Tencent), ODD does not (e.g. 0005.HK -> no, echoing HSBC). This mirrors
    the real market where HK quarterly disclosure is voluntary; it is a
    documented mock simplification, not exchange data."""
    return bool(symbol) and symbol[-1].isdigit() and int(symbol[-1]) % 2 == 0

FIXTURE_FILES = {
    "html_en": ("mock_report_en.html", "text/html", "en"),
    "html_zh": ("mock_report_zh.html", "text/html", "zh"),
    "pdf": ("mock_report.pdf", "application/pdf", None),
}

# Deterministic, synthetic quarter-end periods (clearly not real exchange
# data). Enough unique entries that last_n=20 with a single form still yields
# 20 distinct logical reports (no duplicated report IDs).
def _quarter_ends(count: int = 48) -> list[str]:
    days = {3: 31, 6: 30, 9: 30, 12: 31}
    out: list[str] = []
    year, month = 2026, 6
    for _ in range(count):
        out.append(f"{year:04d}-{month:02d}-{days[month]:02d}")
        month -= 3
        if month <= 0:
            month += 12
            year -= 1
    return out


_PERIODS = _quarter_ends()

SCENARIO_INFO = {
    "success": "All symbols succeed; each yields last_n usable report(s). HK issuers without voluntary quarterly disclosure just skip QTR-HK (symbol status stays succeeded); an explicit HK=[\"QTR-HK\"] request for such an issuer yields no_reports (a normal empty result).",
    "partial": "Each symbol yields usable file(s) plus a null report_period "
               "(period_source=unknown) warning; job status=partial.",
    "failed": "GET job returns HTTP 200 but job status=failed with "
              "source_unavailable (retryable) and no reports.",
    "no_reports": "Job succeeds but each symbol status=no_reports "
                  "(no_matching_reports); no files.",
    "queue_full": "POST returns 429 + Retry-After immediately; no job created.",
    "slow": "Like success but the queued/running phase is stretched by "
            "MOCK_SLOW_MS so a client timeout can be tested with a finite "
            "budget. The job does still reach a terminal state.",
}
DEFAULT_SCENARIO = "success"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _load_fixtures() -> dict:
    fixtures = {}
    for key, (filename, media_type, language) in FIXTURE_FILES.items():
        data = (FIXTURE_DIR / filename).read_bytes()
        fixtures[key] = {
            "filename": filename,
            "media_type": media_type,
            "language": language,
            "bytes": data,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return fixtures


FIXTURES = _load_fixtures()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def _encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(str(seq).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return int(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:  # noqa: BLE001 - any decode failure is an invalid cursor
        return None


def normalize_symbol(raw):
    """Local market detection only; mirrors the real service's shape checks."""
    if not isinstance(raw, str):
        raise ValueError("symbol 必须是字符串")
    s = raw.strip()
    if not s:
        raise ValueError("symbol 不能为空")
    if len(s) > 32:
        raise ValueError("symbol 超过 32 字符")
    up = s.upper()
    if up.endswith(".HK"):
        core = up[:-3]
        if core.isdigit() and 1 <= len(core) <= 5:
            return "HK", core.zfill(5)
        raise ValueError("非法港股代码")
    if up.isdigit():
        if len(up) <= 5:
            return "HK", up.zfill(5)
        if len(up) == 6:
            return "CN", up
        raise ValueError("非法代码")
    if _US_RE.match(up):
        return "US", up
    raise ValueError("无法识别的代码")


def effective_forms(forms_by_market):
    effective = {}
    for market in ("CN", "HK", "US"):
        explicit = (forms_by_market or {}).get(market)
        effective[market] = sorted(explicit) if explicit is not None \
            else list(DEFAULT_FORMS[market])
    return effective


def canonical_request(normalized, last_n, refresh, forms_by_market):
    return {
        "symbols": [f"{market}:{symbol}" for market, symbol in normalized],
        "last_n": last_n,
        "forms_by_market": effective_forms(forms_by_market),
        "refresh": bool(refresh),
    }


def request_hash(canonical) -> str:
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _period_source(market: str, doc_type: str) -> str:
    """Mirror the real service: CN titles and HK quarterly announcements carry
    an explicit period in the title; US reports use a source field."""
    if market == "CN":
        return "explicit_title"
    if market == "HK" and doc_type == "QTR-HK":
        return "explicit_title"
    return "source_field"


def _coverage(requested: int, selected: int, *, exhausted: bool = True,
              truncated: bool = False, notices=None) -> dict:
    """Same keys as the authoritative FetchResult.coverage (v1.0.2 core)."""
    return {
        "requested": requested,
        "selected": selected,
        "total_groups": selected,
        "exhausted": exhausted,
        "truncated": truncated,
        "searched_from": None,
        "searched_to": None,
        "insufficient_history": bool(selected) and selected < requested,
        "notices": list(notices or []),
    }


def _filing_date(period):
    if not period:
        return None
    return (datetime.strptime(period, "%Y-%m-%d")
            + timedelta(days=45)).strftime("%Y-%m-%d")


def _fixture_for(market: str, language: str) -> dict:
    if market in ("CN", "HK"):
        return FIXTURES["pdf"]
    return FIXTURES["html_zh"] if language == "zh" else FIXTURES["html_en"]


def _report_specs(scenario: str, market: str, symbol: str, last_n: int, forms):
    market_forms = list(forms.get(market) or DEFAULT_FORMS[market])
    if market == "HK" and not _hk_has_quarterly(symbol):
        # Issuer does not publish voluntary quarterly results: the default
        # query just skips QTR-HK (not an error). An explicit QTR-HK-only
        # request then legitimately yields no matching reports.
        market_forms = [f for f in market_forms if f != "QTR-HK"]
    if not market_forms:
        return []   # explicit form the issuer never discloses -> no_reports
    specs = []
    if scenario == "partial":
        for i in range(max(0, last_n - 1)):
            specs.append((market_forms[i % len(market_forms)],
                          _PERIODS[i],
                          _period_source(market, market_forms[i % len(market_forms)]),
                          []))
        specs.append((market_forms[0], None, "unknown",
                      ["报告期未知（period_source=unknown）"]))
    else:  # success / slow
        for i in range(last_n):
            specs.append((market_forms[i % len(market_forms)],
                          _PERIODS[i],
                          _period_source(market, market_forms[i % len(market_forms)]),
                          []))
    return specs


def _logical_key(market, symbol, doc_type, period):
    return f"{market}|{symbol}|{doc_type}|{period or 'unknown'}"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class MockState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.idempotency: dict[tuple, str] = {}
        self.reports: dict[str, dict] = {}
        self.artifacts: dict[str, dict] = {}
        self.report_order: list[str] = []
        self.next_seq = 1

    def reset(self) -> None:
        with self.lock:
            self.jobs.clear()
            self.idempotency.clear()
            self.reports.clear()
            self.artifacts.clear()
            self.report_order.clear()
            self.next_seq = 1


def _make_report(state: MockState, market, symbol, doc_type, period,
                 period_source, warnings):
    key = _logical_key(market, symbol, doc_type, period)
    report_id = "r_" + hashlib.sha1(key.encode()).hexdigest()[:12]
    artifact_id = "a_" + hashlib.sha1((key + "|v1").encode()).hexdigest()[:12]
    language = "zh" if market == "CN" else "en"
    fixture = _fixture_for(market, language)
    fetched_at = _now_iso()
    artifact = {
        "artifact_id": artifact_id,
        "report_id": report_id,
        "sha256": fixture["sha256"],
        "bytes": len(fixture["bytes"]),
        "media_type": fixture["media_type"],
        "fetched_at": fetched_at,
        "state": "ready",
        "filename": fixture["filename"],
        "content": fixture["bytes"],
    }
    report = {
        "report_id": report_id,
        "market": market,
        "symbol": symbol,
        "issuer_id": None,
        "source_id": f"mock:{market}:{symbol}:{doc_type}:{period or 'unknown'}",
        "source_url": (f"https://mock.invalid/{market}/{symbol}/"
                       f"{doc_type}/{period or 'unknown'}"),
        "title": (f"[MOCK] {symbol} {doc_type} synthetic fixture "
                  f"(not real financial information)"),
        "doc_type": doc_type,
        "source_form": doc_type,
        "report_period": period,
        "period_source": period_source,
        "filing_date": _filing_date(period),
        "language": language,
        "is_amendment": False,
        "revision_of": None,
        "status": "done",
        "warnings": list(warnings),
        "current_artifact_id": artifact_id,
        "artifact": artifact,
        "seq": state.next_seq,
    }
    state.next_seq += 1
    state.reports[report_id] = report
    state.artifacts[artifact_id] = artifact
    state.report_order.append(report_id)
    return report


def _compute_terminal(state: MockState, job: dict):
    scenario = job["scenario"]
    last_n = job["last_n"]
    if scenario == "failed":
        results = [{
            "market": market, "symbol": symbol, "display_name": None,
            "status": "failed", "report_ids": [], "items": [],
            "coverage": _coverage(last_n, 0, exhausted=False),
            "warnings": [],
            "error": {"code": "source_unavailable", "retryable": True},
        } for market, symbol in job["symbols"]]
        return results, {"downloaded": 0, "cached": 0, "failed": 0,
                         "error": {"code": "source_unavailable",
                                   "retryable": True}}
    if scenario == "no_reports":
        results = [{
            "market": market, "symbol": symbol, "display_name": None,
            "status": "no_reports", "report_ids": [], "items": [],
            "coverage": _coverage(last_n, 0, exhausted=True),
            "warnings": ["no_matching_reports"],
            "error": None,
        } for market, symbol in job["symbols"]]
        return results, {"downloaded": 0, "cached": 0, "failed": 0}

    downloaded = cached = 0
    results = []
    for market, symbol in job["symbols"]:
        items = []
        symbol_warnings: list[str] = []
        specs = _report_specs(scenario, market, symbol, last_n, job["forms"])
        if not specs:
            # Normal empty terminal state (e.g. explicit QTR-HK for an issuer
            # without voluntary quarterly disclosure) — not a failure.
            results.append({
                "market": market, "symbol": symbol, "display_name": None,
                "status": "no_reports", "report_ids": [], "items": [],
                "coverage": _coverage(last_n, 0, exhausted=True),
                "warnings": ["no_matching_reports"],
                "error": None,
            })
            continue
        for doc_type, period, period_source, warns in specs:
            key = _logical_key(market, symbol, doc_type, period)
            report_id = "r_" + hashlib.sha1(key.encode()).hexdigest()[:12]
            report = state.reports.get(report_id)
            if report is None:
                report = _make_report(state, market, symbol, doc_type, period,
                                      period_source, warns)
                outcome = "downloaded"
                downloaded += 1
            else:
                outcome = "cached"
                cached += 1
            items.append({
                "report_id": report_id,
                "source_id": report["source_id"],
                "status": outcome,
                "artifact_id": report["current_artifact_id"],
            })
            symbol_warnings.extend(warns)
        status = "partial" if scenario == "partial" else "succeeded"
        results.append({
            "market": market, "symbol": symbol, "display_name": None,
            "status": status,
            "report_ids": [i["report_id"] for i in items],
            "items": items,
            "coverage": _coverage(last_n, len(items), exhausted=True),
            "warnings": symbol_warnings,
            "error": None,
        })
    return results, {"downloaded": downloaded, "cached": cached, "failed": 0}


def _refresh_terminal(state: MockState, job: dict) -> None:
    """Advance a job deterministically from elapsed wall-clock time."""
    if job["results"] is not None:
        return
    elapsed = time.monotonic() - job["submitted_monotonic"]
    if elapsed >= job["terminal_after"]:
        results, summary = _compute_terminal(state, job)
        job["results"] = results
        job["summary"] = summary
        job["status"] = "failed" if job["scenario"] == "failed" else (
            "partial" if job["scenario"] == "partial" else "succeeded")
        job["attempt"] = 1
        job["started_at"] = job["started_at"] or _now_iso()
        job["finished_at"] = _now_iso()
    elif elapsed >= job["terminal_after"] / 2.0:
        job["status"] = "running"
        job["attempt"] = 1
        if job["started_at"] is None:
            job["started_at"] = _now_iso()
    else:
        job["status"] = "queued"


def _job_document(job: dict) -> dict:
    finished = job["results"] is not None
    results = job["results"] if finished else []
    summary = job["summary"] if finished else {
        "downloaded": 0, "cached": 0, "failed": 0}
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "attempt": job["attempt"],
        "submitted_at": job["submitted_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "deadline": job["deadline"],
        "progress": {"symbols_total": len(job["symbols"]),
                     "symbols_finished": len(results) if finished else 0},
        "summary": summary,
        "results": results,
    }


def get_job_document(state: MockState, job_id: str, client_id: str):
    with state.lock:
        job = state.jobs.get(job_id)
        if job is None or job["client_id"] != client_id:
            return None
        _refresh_terminal(state, job)
        return _job_document(job)


def _validate_submit_body(body):
    """Return (errors, normalized, last_n, refresh, forms)."""
    errors: list[dict] = []
    if not isinstance(body, dict):
        return ([{"field": "body", "detail": "请求体必须是 JSON 对象"}],
                [], 4, False, None)
    allowed = {"symbols", "last_n", "forms_by_market", "refresh"}
    for extra in body.keys():
        if extra not in allowed:
            errors.append({"field": extra, "detail": "未知字段不被接受"})

    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    symbols = body.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        errors.append({"field": "symbols", "detail": "symbols 必填且至少 1 项"})
    elif len(symbols) > 50:
        errors.append({"field": "symbols", "detail": "symbols 最多 50 项"})
    else:
        for idx, raw in enumerate(symbols):
            try:
                norm = normalize_symbol(raw)
            except ValueError as exc:
                errors.append({"field": f"symbols[{idx}]", "detail": str(exc)})
                continue
            # Aliases collapse to one normalized symbol, keeping input order,
            # so the canonical request hash matches production behaviour.
            if norm in seen:
                continue
            seen.add(norm)
            normalized.append(norm)

    last_n = body.get("last_n", 4)
    if isinstance(last_n, bool) or not isinstance(last_n, int) \
            or not (1 <= last_n <= 20):
        errors.append({"field": "last_n", "detail": "last_n 须为 1..20 的整数"})

    refresh = body.get("refresh", False)
    if not isinstance(refresh, bool):
        errors.append({"field": "refresh", "detail": "refresh 须为布尔值"})

    forms = body.get("forms_by_market")
    if forms is not None:
        if not isinstance(forms, dict):
            errors.append({"field": "forms_by_market", "detail": "须为对象"})
        else:
            for market, value in forms.items():
                if market not in SUPPORTED_FORMS:
                    errors.append({"field": f"forms_by_market.{market}",
                                   "detail": "未知市场（支持 CN/HK/US）"})
                    continue
                if not isinstance(value, list) or not value:
                    errors.append({"field": f"forms_by_market.{market}",
                                   "detail": "空数组不被接受"})
                    continue
                unknown = [f for f in value
                           if f not in SUPPORTED_FORMS[market]]
                if unknown:
                    errors.append({"field": f"forms_by_market.{market}",
                                   "detail": f"不支持的基础类型 {unknown}"})
    return errors, normalized, last_n, refresh, forms


def submit_job(state: MockState, client_id: str, key: str, body: dict,
               scenario: str, phase_ms: int, slow_ms: int):
    errors, normalized, last_n, refresh, forms = _validate_submit_body(body)
    if errors:
        return "invalid", errors, None
    canonical = canonical_request(normalized, last_n, refresh, forms)
    req_hash = request_hash(canonical)
    now = _now_iso()
    with state.lock:
        existing_id = state.idempotency.get((client_id, key))
        if existing_id is not None:
            job = state.jobs[existing_id]
            if job["request_hash"] != req_hash:
                return "conflict", None, job
            _refresh_terminal(state, job)
            return "replay", None, job
        job_id = "job_" + uuid.uuid4().hex[:20]
        terminal_after = (slow_ms / 1000.0) if scenario == "slow" \
            else (2 * phase_ms / 1000.0)
        job = {
            "job_id": job_id,
            "client_id": client_id,
            "key": key,
            "request_hash": req_hash,
            "canonical": canonical,
            "scenario": scenario,
            "symbols": normalized,
            "last_n": last_n,
            "refresh": bool(refresh),
            "forms": effective_forms(forms),
            "status": "queued",
            "attempt": 0,
            "submitted_at": now,
            "started_at": None,
            "finished_at": None,
            "deadline": (_utcnow_dt() + timedelta(minutes=30)).isoformat(
                timespec="seconds"),
            "submitted_monotonic": time.monotonic(),
            "terminal_after": terminal_after,
            "results": None,
            "summary": None,
        }
        state.jobs[job_id] = job
        state.idempotency[(client_id, key)] = job_id
        return "created", None, job


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def _download_filename(report: dict, artifact: dict) -> str:
    """Readable, deterministic attachment name mirroring the real service.

    ``market_symbol_doc_type_<period|filing|unknown>_report_id.ext``; a
    historical (non-current) artifact appends its artifact_id. ASCII fallback
    plus RFC 6266 ``filename*`` UTF-8 are both provided.
    """
    period_token = report["report_period"] or report["filing_date"] or "unknown"
    parts = [report["market"], report["symbol"], report["doc_type"],
             period_token, report["report_id"]]
    base = "_".join(re.sub(r"[^A-Za-z0-9._-]+", "-", str(p)).strip("-._") or
                    "unknown" for p in parts)
    if artifact["artifact_id"] != report["current_artifact_id"]:
        base = f"{base}_{artifact['artifact_id']}"
    ext = "pdf" if artifact["media_type"] == "application/pdf" else "html"
    name = f"{base}.{ext}"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name) or "report"
    return (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(name)}")


def _report_list_item(report: dict) -> dict:
    artifact = report["artifact"]
    return {
        "report_id": report["report_id"],
        "market": report["market"],
        "symbol": report["symbol"],
        "issuer_id": report["issuer_id"],
        "source_id": report["source_id"],
        "source_url": report["source_url"],
        "title": report["title"],
        "doc_type": report["doc_type"],
        "report_period": report["report_period"],
        "period_source": report["period_source"],
        "filing_date": report["filing_date"],
        "language": report["language"],
        "is_amendment": report["is_amendment"],
        "status": report["status"],
        "artifact_id": report["current_artifact_id"],
        "sha256": artifact["sha256"],
        "bytes": artifact["bytes"],
        "fetched_at": artifact["fetched_at"],
        "warnings": list(report["warnings"]),
        "download_url": f"/api/v1/reports/{report['report_id']}/file",
    }


def _artifact_out(report: dict) -> dict:
    artifact = report["artifact"]
    return {
        "artifact_id": artifact["artifact_id"],
        "sha256": artifact["sha256"],
        "bytes": artifact["bytes"],
        "media_type": artifact["media_type"],
        "fetched_at": artifact["fetched_at"],
        "state": artifact["state"],
        "is_current": artifact["artifact_id"] == report["current_artifact_id"],
    }


def _report_detail(report: dict) -> dict:
    return {
        "report_id": report["report_id"],
        "market": report["market"],
        "symbol": report["symbol"],
        "source_id": report["source_id"],
        "source_url": report["source_url"],
        "title": report["title"],
        "doc_type": report["doc_type"],
        "source_form": report["source_form"],
        "report_period": report["report_period"],
        "period_source": report["period_source"],
        "filing_date": report["filing_date"],
        "language": report["language"],
        "is_amendment": report["is_amendment"],
        "revision_of": report["revision_of"],
        "status": report["status"],
        "current_artifact_id": report["current_artifact_id"],
        "warnings": list(report["warnings"]),
        "artifacts": [_artifact_out(report)],
    }


def list_reports(state: MockState, *, market=None, symbol=None, doc_type=None,
                 period_from=None, period_to=None, limit=20, cursor=None):
    if not (1 <= limit <= 100):
        raise HttpProblem(422, "invalid_request", "Unprocessable Content",
                          "limit 范围为 1-100")
    if market and market not in ("CN", "HK", "US"):
        raise HttpProblem(422, "invalid_request", "Unprocessable Content",
                          "未知市场")
    for label, value in (("period_from", period_from),
                         ("period_to", period_to)):
        if value and not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            raise HttpProblem(422, "invalid_request", "Unprocessable Content",
                              f"{label} 须为 YYYY-MM-DD")
    max_seq = None
    if cursor:
        max_seq = _decode_cursor(cursor)
        if max_seq is None:
            raise HttpProblem(400, "invalid_cursor", "Bad Request", "无效游标")

    with state.lock:
        rows = [state.reports[r] for r in state.report_order]
    rows.sort(key=lambda r: r["seq"], reverse=True)
    selected = []
    for report in rows:
        if market and report["market"] != market:
            continue
        if symbol and report["symbol"] != symbol:
            continue
        if doc_type and report["doc_type"] != doc_type:
            continue
        if period_from and (not report["report_period"]
                            or report["report_period"] < period_from):
            continue
        if period_to and (not report["report_period"]
                          or report["report_period"] > period_to):
            continue
        if max_seq is not None and report["seq"] >= max_seq:
            continue
        selected.append(report)
    has_more = len(selected) > limit
    page = selected[:limit]
    next_cursor = _encode_cursor(page[-1]["seq"]) if has_more and page else None
    return {"items": [_report_list_item(r) for r in page],
            "next_cursor": next_cursor}


def get_report_file(state: MockState, report_id: str, artifact_id):
    with state.lock:
        if artifact_id is not None:
            artifact = state.artifacts.get(artifact_id)
            if artifact is None or artifact["report_id"] != report_id:
                raise HttpProblem(
                    404, "not_found", "Not Found",
                    f"artifact 不存在或不属于该报告: {artifact_id}")
            report = state.reports.get(report_id)
        else:
            report = state.reports.get(report_id)
            if report is None:
                raise HttpProblem(404, "not_found", "Not Found",
                                  f"报告不存在: {report_id}")
            if not report["current_artifact_id"]:
                raise HttpProblem(409, "file_not_available", "Conflict",
                                  "当前无可用文件版本")
            artifact = state.artifacts[report["current_artifact_id"]]
        return report, artifact


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class HttpProblem(Exception):
    def __init__(self, status, code, title, detail, *, retryable=False,
                 errors=None, headers=None):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
        self.errors = errors
        self.headers = headers or {}


class Resp:
    def __init__(self, status=200, body=b"",
                 content_type="application/json; charset=utf-8",
                 headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


DOCS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>reports-fetcher local contract kit - docs</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem;
      max-width:960px;line-height:1.55;color:#1a1a1a}
 code{background:#f2f2f2;padding:.1em .35em;border-radius:4px}
 table{border-collapse:collapse;width:100%;margin:1rem 0}
 th,td{border:1px solid #ccc;padding:.45rem .6rem;text-align:left;
       vertical-align:top}
 th{background:#f6f6f6}
 .note{background:#fff7e0;border:1px solid #e6c200;padding:.75rem 1rem;
       border-radius:6px}
 .method{font-weight:700}
</style></head>
<body>
<h1>reports-fetcher local contract kit</h1>
<p>This is a fully offline mock of the reports-fetcher v1.0.3 HTTP API.
No CDN, no internet, no production calls. Corrected machine-readable contract:
<a href="/openapi.json"><code>/openapi.json</code></a>.</p>
<div class="note"><strong>Mock-only control surface:</strong>
<code>X-Mock-Scenario</code> request header, <code>POST /__mock/reset</code>,
<code>GET /__mock/scenarios</code>. These MUST never be sent to production.</div>
<p>Routes (static list, also rendered from the OpenAPI below):
<code>POST /api/v1/fetch-jobs</code>,
<code>GET /api/v1/fetch-jobs/{job_id}</code>,
<code>GET /api/v1/reports</code>,
<code>GET /api/v1/reports/{report_id}</code>,
<code>GET /api/v1/reports/{report_id}/file</code>,
<code>GET /health/live</code>,
<code>GET /health/ready</code>.</p>
<h2>Operations</h2>
<table><thead><tr><th>Method</th><th>Path</th><th>Summary</th></tr></thead>
<tbody id="ops"><tr><td colspan="3">loading /openapi.json ...</td></tr></tbody>
</table>
<h2>Scenarios</h2>
<table><thead><tr><th>Name</th><th>Behaviour</th></tr></thead>
<tbody id="scenarios"><tr><td colspan="2">loading ...</td></tr></tbody>
</table>
<script>
async function load(){
  try{
    const spec = await (await fetch('/openapi.json')).json();
    const rows=[];
    for(const [path,methods] of Object.entries(spec.paths||{})){
      for(const [method,op] of Object.entries(methods)){
        rows.push('<tr><td class="method">'+method.toUpperCase()+
          '</td><td><code>'+path+'</code></td><td>'+
          (op.summary||'')+'</td></tr>');
      }
    }
    document.getElementById('ops').innerHTML=rows.join('');
  }catch(e){
    document.getElementById('ops').innerHTML=
      '<tr><td colspan="3">openapi.json not available</td></tr>';
  }
  try{
    const data = await (await fetch('/__mock/scenarios')).json();
    document.getElementById('scenarios').innerHTML = data.scenarios
      .map(s=>'<tr><td><code>'+s.name+'</code></td><td>'+s.description+
        '</td></tr>').join('');
  }catch(e){
    document.getElementById('scenarios').innerHTML=
      '<tr><td colspan="2">scenarios not available</td></tr>';
  }
}
load();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "reports-fetcher-mock/1.0"

    def log_message(self, fmt, *args):  # quieter default
        sys.stderr.write("[mock] " + (fmt % args) + "\n")

    # -- entry points ------------------------------------------------------ #

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        self._request_id = self.headers.get("X-Request-ID") or \
            f"req_{uuid.uuid4().hex[:16]}"
        self._path = path
        try:
            if method == "POST":
                resp = self._dispatch_post(path, query)
            else:
                resp = self._dispatch_get(path, query)
        except HttpProblem as problem:
            resp = self._problem_response(problem)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[mock] unexpected error: {exc!r}\n")
            resp = self._problem_response(HttpProblem(
                500, "internal_error", "Internal Server Error",
                "mock 内部错误（不泄露堆栈）"))
        self._send(resp, head_only=(method == "HEAD"))

    def _send(self, resp: Resp, head_only=False):
        body = resp.body
        self.send_response(resp.status)
        if resp.content_type:
            self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self._request_id)
        for name, value in resp.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _json(self, status, payload, headers=None):
        return Resp(status,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8", headers)

    def _problem_response(self, problem: HttpProblem) -> Resp:
        body = {
            "type": "about:blank",
            "title": problem.title,
            "status": problem.status,
            "detail": problem.detail,
            "instance": self._path,
            "code": problem.code,
            "request_id": self._request_id,
            "retryable": problem.retryable,
        }
        if problem.errors:
            body["errors"] = problem.errors
        return Resp(problem.status,
                    json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    "application/problem+json", problem.headers)

    # -- auth (optional, mock-only) ---------------------------------------- #

    def _client_id(self) -> str:
        token = os.environ.get("MOCK_API_TOKEN", "").strip()
        if not token:
            return "local"
        header = self.headers.get("Authorization", "")
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise HttpProblem(401, "unauthorized", "Unauthorized",
                              "缺少 Bearer 凭据",
                              headers={"WWW-Authenticate": "Bearer"})
        if credential.strip() != token:
            raise HttpProblem(403, "forbidden", "Forbidden", "凭据无效",
                              headers={"WWW-Authenticate": "Bearer"})
        return "local"

    # -- routing ----------------------------------------------------------- #

    def _dispatch_get(self, path, query):
        if path in ("/health/live", "/health/ready"):
            return self._json(200, {"status": "ok"})
        if path == "/":
            return Resp(200, DOCS_HTML.encode("utf-8"),
                        "text/html; charset=utf-8")
        if path == "/docs":
            return Resp(200, DOCS_HTML.encode("utf-8"),
                        "text/html; charset=utf-8")
        if path == "/openapi.json":
            if not OPENAPI_PATH.exists():
                raise HttpProblem(500, "internal_error", "Internal Server Error",
                                  "openapi.json 缺失")
            return Resp(200, OPENAPI_PATH.read_bytes(),
                        "application/json; charset=utf-8")
        if path == "/__mock/scenarios":
            return self._json(200, self._scenarios_payload())
        if path.startswith("/api/v1/"):
            self._client_id()
            return self._api_get(path, query)
        raise HttpProblem(404, "not_found", "Not Found", f"未知路径: {path}")

    def _dispatch_post(self, path, query):
        if path == "/__mock/reset":
            self.server.state.reset()
            return self._json(200, {"status": "ok", "reset": True})
        if path == "/api/v1/fetch-jobs":
            self._client_id()
            return self._submit(path)
        raise HttpProblem(404, "not_found", "Not Found", f"未知路径: {path}")

    def _api_get(self, path, query):
        if path == "/api/v1/reports":
            params = {k: v[0] for k, v in query.items() if v}
            limit_raw = params.get("limit", "20")
            try:
                limit = int(limit_raw)
            except ValueError:
                raise HttpProblem(422, "invalid_request",
                                  "Unprocessable Content", "limit 须为整数")
            result = list_reports(
                self.server.state,
                market=params.get("market"),
                symbol=params.get("symbol"),
                doc_type=params.get("doc_type"),
                period_from=params.get("period_from"),
                period_to=params.get("period_to"),
                limit=limit,
                cursor=params.get("cursor"))
            return self._json(200, result)

        if path.startswith("/api/v1/reports/") and path.endswith("/file"):
            report_id = path[len("/api/v1/reports/"):-len("/file")]
            artifact_id = (query.get("artifact_id") or [None])[0]
            return self._report_file(report_id, artifact_id)

        if path.startswith("/api/v1/reports/"):
            report_id = path[len("/api/v1/reports/"):]
            with self.server.state.lock:
                report = self.server.state.reports.get(report_id)
            if report is None:
                raise HttpProblem(404, "not_found", "Not Found",
                                  f"报告不存在: {report_id}")
            return self._json(200, _report_detail(report))

        if path.startswith("/api/v1/fetch-jobs/"):
            job_id = path[len("/api/v1/fetch-jobs/"):]
            doc = get_job_document(self.server.state, job_id,
                                   self._client_id())
            if doc is None:
                raise HttpProblem(404, "not_found", "Not Found",
                                  f"任务不存在: {job_id}")
            return self._json(200, doc)

        raise HttpProblem(404, "not_found", "Not Found", f"未知路径: {path}")

    # -- submit ------------------------------------------------------------ #

    def _read_body(self):
        header = self.headers.get("Content-Length")
        if header is None:
            return b""
        try:
            length = int(header)
        except ValueError:
            raise HttpProblem(400, "invalid_request", "Bad Request",
                              "无效的 Content-Length")
        if length > REQUEST_BODY_LIMIT:
            raise HttpProblem(413, "payload_too_large", "Content Too Large",
                              f"请求体超过 {REQUEST_BODY_LIMIT} 字节")
        return self.rfile.read(length)

    def _submit(self, path):
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip() != "application/json":
            raise HttpProblem(415, "unsupported_media_type",
                              "Unsupported Media Type",
                              "Content-Type 必须为 application/json")
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpProblem(422, "invalid_request", "Unprocessable Content",
                              "请求体不是合法 JSON")
        key = self.headers.get("Idempotency-Key", "")
        if not key:
            raise HttpProblem(400, "missing_idempotency_key", "Bad Request",
                              "缺少 Idempotency-Key")
        if not _IDEMPOTENCY_RE.match(key):
            raise HttpProblem(400, "invalid_idempotency_key", "Bad Request",
                              "Idempotency-Key 须为 1-128 个可打印 ASCII 字符")
        scenario = self.headers.get("X-Mock-Scenario") or DEFAULT_SCENARIO
        if scenario not in SCENARIO_INFO:
            raise HttpProblem(
                422, "invalid_request", "Unprocessable Content",
                f"未知 mock 场景 {scenario!r}（见 /__mock/scenarios）")
        if scenario == "queue_full":
            raise HttpProblem(429, "queue_full", "Too Many Requests",
                              "mock 队列已满（X-Mock-Scenario: queue_full）",
                              retryable=True, headers={"Retry-After": "10"})
        client_id = self._client_id()
        outcome, errors, job = submit_job(
            self.server.state, client_id, key, body, scenario,
            self.server.phase_ms, self.server.slow_ms)
        if outcome == "invalid":
            raise HttpProblem(422, "invalid_request", "Unprocessable Content",
                              "请求参数校验失败", errors=errors)
        if outcome == "conflict":
            raise HttpProblem(409, "idempotency_conflict", "Conflict",
                              "相同幂等键已被不同请求占用（HTTP_API §4）")
        finished = job["results"] is not None
        status = 200 if finished else 202
        headers = {
            "Location": f"/api/v1/fetch-jobs/{job['job_id']}",
            "Retry-After": "2",
        }
        return self._json(status, {
            "job_id": job["job_id"],
            "status": job["status"],
            "submitted_at": job["submitted_at"],
            "status_url": f"/api/v1/fetch-jobs/{job['job_id']}",
        }, headers)

    # -- file -------------------------------------------------------------- #

    def _report_file(self, report_id, artifact_id):
        report, artifact = get_report_file(self.server.state, report_id,
                                           artifact_id)
        etag = f'"{artifact["sha256"]}"'
        if self.headers.get("If-None-Match") == etag:
            return Resp(304, b"", None, {"ETag": etag})
        ext = "pdf" if artifact["media_type"] == "application/pdf" else "html"
        filename = _download_filename(report, artifact)
        return Resp(200, artifact["content"], artifact["media_type"], {
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": filename,
        })

    # -- mock control ------------------------------------------------------ #

    def _scenarios_payload(self):
        return {
            "default_scenario": DEFAULT_SCENARIO,
            "header": "X-Mock-Scenario",
            "terminal_statuses": list(TERMINAL_STATUSES),
            "scenarios": [
                {"name": name, "description": desc}
                for name, desc in SCENARIO_INFO.items()
            ],
            "control_routes": [
                "POST /__mock/reset",
                "GET /__mock/scenarios",
            ],
            "warning": ("Mock-only surface. Never send X-Mock-Scenario or "
                        "/__mock/* to production."),
            "slow_ms": self.server.slow_ms,
            "phase_ms": self.server.phase_ms,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description="reports-fetcher local mock")
    parser.add_argument("--host", default=os.environ.get("MOCK_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MOCK_PORT", "8080")))
    parser.add_argument("--phase-ms", type=int,
                        default=int(os.environ.get("MOCK_PHASE_MS", "500")))
    parser.add_argument("--slow-ms", type=int,
                        default=int(os.environ.get("MOCK_SLOW_MS", "8000")))
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.state = MockState()
    server.phase_ms = args.phase_ms
    server.slow_ms = args.slow_ms
    sys.stderr.write(
        f"[mock] listening on {args.host}:{args.port} "
        f"(phase_ms={args.phase_ms}, slow_ms={args.slow_ms})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
