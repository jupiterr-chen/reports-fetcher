#!/usr/bin/env python3
"""Runnable example client for the reports-fetcher API.

It submits one fetch job, polls it to a terminal state with a bounded timeout,
downloads every usable file, verifies each SHA-256 checksum, and reports
warnings. It works unchanged against the local mock and against a real
deployment (only ``REPORTS_API_BASE_URL`` / ``REPORTS_API_TOKEN`` change).

Run::

    python -m client.example_client

Environment:
    REPORTS_API_BASE_URL   default http://127.0.0.1:18765 (local mock)
    REPORTS_API_TOKEN      optional bearer token (unset in local mode)
    REPORTS_CLIENT_SYMBOLS comma separated, default "AAPL"
    REPORTS_CLIENT_LAST_N  default 4
    REPORTS_CLIENT_TIMEOUT overall poll budget seconds, default 60
    REPORTS_CLIENT_POLL    poll interval seconds, default 0.5
    REPORTS_CLIENT_OUT     output dir, default "examples/out" (kit-local)
    REPORTS_MOCK_SCENARIO  MOCK ONLY. Never set against production.

Exit codes: 0 success, 1 partial (usable files + warnings),
            2 failed job or checksum mismatch, 3 timeout, 4 HTTP/connection.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from client.reports_client import (
    ClientError,
    ClientTimeout,
    ConnectionFailed,
    ProblemError,
    ReportsClient,
)

_EXT = {"application/pdf": "pdf", "text/html": "html"}


def _ext_for(media_type: str) -> str:
    if not media_type:
        return "bin"
    base = media_type.split(";")[0].strip().lower()
    return _EXT.get(base, "bin")


def main(argv=None) -> int:
    base_url = os.environ.get("REPORTS_API_BASE_URL", "http://127.0.0.1:18765")
    token = os.environ.get("REPORTS_API_TOKEN") or None
    symbols = [s.strip() for s in
               os.environ.get("REPORTS_CLIENT_SYMBOLS", "AAPL").split(",")
               if s.strip()]
    last_n = int(os.environ.get("REPORTS_CLIENT_LAST_N", "4"))
    budget = float(os.environ.get("REPORTS_CLIENT_TIMEOUT", "60"))
    poll = float(os.environ.get("REPORTS_CLIENT_POLL", "0.5"))
    out_dir = Path(os.environ.get("REPORTS_CLIENT_OUT", "examples/out"))
    scenario = os.environ.get("REPORTS_MOCK_SCENARIO") or None

    print(f"base_url={base_url} symbols={symbols} last_n={last_n} "
          f"budget={budget}s")
    if scenario:
        print(f"[WARN] X-Mock-Scenario={scenario!r} is a MOCK-ONLY header. "
              "Never send it to production.")
    if token:
        print("auth: bearer token configured")
    else:
        print("auth: none (local/loopback mode)")

    client = ReportsClient(base_url, token, max_wait=budget, poll_interval=poll)

    try:
        client.health_ready()
        status_code, accepted = client.submit_job(
            symbols, last_n=last_n, scenario=scenario)
        job_id = accepted["job_id"]
        print(f"submitted: http={status_code} job_id={job_id} "
              f"status={accepted['status']}")
    except ClientTimeout as exc:
        print(f"[TIMEOUT] readiness/submit exceeded the {budget}s budget: "
              f"{exc}", file=sys.stderr)
        return 3
    except ProblemError as exc:
        print(f"[ERROR] submit failed: {exc}", file=sys.stderr)
        return 4
    except ConnectionFailed as exc:
        print(f"[ERROR] cannot reach {base_url}: {exc}", file=sys.stderr)
        return 4

    try:
        doc = client.wait_for_terminal(job_id, timeout=budget)
    except ClientTimeout as exc:
        last = exc.last_document or {}
        print(f"[TIMEOUT] job {job_id} still {last.get('status')} after "
              f"{budget}s; stopping (bounded wait).", file=sys.stderr)
        return 3
    except ClientError as exc:
        print(f"[ERROR] polling failed: {exc}", file=sys.stderr)
        return 4

    summary = doc.get("summary", {})
    print(f"terminal: status={doc['status']} summary={summary}")

    for result in doc.get("results", []):
        err = result.get("error")
        if err:
            print(f"  {result['market']}:{result['symbol']} "
                  f"status={result['status']} error={err}")
        for warning in result.get("warnings", []):
            print(f"  [warning] {result['market']}:{result['symbol']} "
                  f"{warning}")

    if doc["status"] == "failed":
        print("[FAILED] job failed; no usable reports.", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    saved = mismatched = 0
    timed_out = False
    for result in doc.get("results", []):
        for report_id in result.get("report_ids", []):
            try:
                detail = client.get_report(report_id)
                artifacts = detail.get("artifacts") or []
                current = next((a for a in artifacts if a.get("is_current")),
                               artifacts[0] if artifacts else None)
                blob = client.download_report_file(report_id)
            except ClientTimeout as exc:
                # Keep whatever was already saved; report a timeout exit code.
                print(f"  [TIMEOUT] {report_id}: {exc}", file=sys.stderr)
                timed_out = True
                continue
            except (ProblemError, ConnectionFailed) as exc:
                print(f"  [ERROR] {report_id}: {exc}", file=sys.stderr)
                mismatched += 1
                continue
            content = blob["content"]
            expected = (current or {}).get("sha256")
            digest = client.sha256_hex(content)
            if expected and digest != expected:
                print(f"  [ERROR] {report_id}: sha256 mismatch "
                      f"(got {digest[:12]} want {expected[:12]})",
                      file=sys.stderr)
                mismatched += 1
                continue
            ext = _ext_for(blob.get("media_type") or (current or {})
                           .get("media_type", ""))
            path = out_dir / f"{report_id}.{ext}"
            path.write_bytes(content)
            saved += 1
            print(f"  saved {path} ({len(content)} bytes, sha256 ok)")
            for warning in detail.get("warnings", []):
                print(f"  [warning] {report_id}: {warning}")

    print(f"downloaded={saved} mismatched={mismatched} out={out_dir}")
    if timed_out:
        return 3
    if mismatched:
        return 2
    if doc["status"] == "partial":
        print("[PARTIAL] usable files downloaded; review warnings above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
