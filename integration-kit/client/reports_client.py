"""Standard-library HTTP client for the reports-fetcher API (v1.0.2 contract).

Usage::

    from client.reports_client import ReportsClient

    client = ReportsClient("http://127.0.0.1:18765")
    status, accepted = client.submit_job(["AAPL"], last_n=4)
    doc = client.wait_for_terminal(accepted["job_id"], timeout=60)
    for result in doc["results"]:
        for report_id in result["report_ids"]:
            blob = client.download_report_file(report_id)
            assert client.sha256_hex(blob["content"]) == \
                client.get_report(report_id)["artifacts"][0]["sha256"]

Design rules:

* The only configuration is ``base_url`` and an optional bearer ``token``.
* Mock-only headers (``X-Mock-Scenario``) are only sent when the caller passes
  ``scenario=`` explicitly. Normal production code must never pass it.
* This module never calls ``/__mock/*`` control routes.
* ``GET /api/v1/fetch-jobs/{id}`` returning HTTP 200 does NOT mean the job
  succeeded: always inspect the ``status`` field (terminal values are
  ``succeeded`` / ``partial`` / ``failed``).
"""
from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode

TERMINAL_STATUSES = ("succeeded", "partial", "failed")


class ClientError(Exception):
    """Base class for client failures."""


class ConnectionFailed(ClientError):
    """The server could not be reached (DNS, refused, TLS, ...)."""


class ClientTimeout(ClientError):
    """The job did not reach a terminal state within the caller budget."""

    def __init__(self, job_id: str, last_document=None):
        self.job_id = job_id
        self.last_document = last_document
        last = (last_document or {}).get("status", "unknown")
        super().__init__(f"job {job_id} still {last} at timeout")


class ProblemError(ClientError):
    """The server returned an ``application/problem+json`` error."""

    def __init__(self, status: int, problem: dict, headers=None):
        self.status = status
        self.problem = problem or {}
        self.headers = headers
        self.code = self.problem.get("code")
        self.retryable = bool(self.problem.get("retryable"))
        detail = self.problem.get("detail") or self.problem.get("title") or ""
        super().__init__(f"HTTP {status} [{self.code}]: {detail}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _header(headers, name, default=None):
    if headers is None:
        return default
    return headers.get(name, default)


class ReportsClient:
    def __init__(self, base_url: str, token: str | None = None, *,
                 timeout: float = 30.0, max_wait: float = 120.0,
                 poll_interval: float = 1.0,
                 user_agent: str = "reports-fetcher-integration-kit/1.0"):
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self.timeout = timeout
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.user_agent = user_agent

    # -- low level --------------------------------------------------------- #

    def _request(self, method: str, path: str, *, headers=None, body=None,
                 timeout=None, if_none_match=None):
        url = self.base_url + path
        merged = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.token:
            merged["Authorization"] = f"Bearer {self.token}"
        if if_none_match is not None:
            merged["If-None-Match"] = if_none_match
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        if headers:
            merged.update(headers)
        request = urllib.request.Request(url, data=data, headers=merged,
                                         method=method)
        try:
            with urllib.request.urlopen(request,
                                        timeout=timeout or self.timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ClientTimeout(path, None) from exc
            raise ConnectionFailed(str(reason)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ClientTimeout(path, None) from exc

    def _raise_problem(self, status, headers, body):
        try:
            problem = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            problem = {"detail": body.decode("utf-8", "replace")[:500]}
        raise ProblemError(status, problem, headers)

    def _json(self, method, path, *, headers=None, body=None, timeout=None):
        status, resp_headers, raw = self._request(
            method, path, headers=headers, body=body, timeout=timeout)
        if status >= 400:
            self._raise_problem(status, resp_headers, raw)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    # -- health ------------------------------------------------------------ #

    def health_live(self) -> dict:
        return self._json("GET", "/health/live")

    def health_ready(self) -> dict:
        return self._json("GET", "/health/ready")

    # -- jobs -------------------------------------------------------------- #

    def submit_job(self, symbols, *, last_n: int = 4, forms_by_market=None,
                   refresh: bool = False, idempotency_key: str | None = None,
                   scenario: str | None = None):
        """POST a fetch job.

        Returns ``(status_code, JobAccepted)``. ``status_code`` is 202 for a new
        or still-pending job and 200 when an idempotent replay hits a terminal
        job. ``scenario`` is mock-only; leave it ``None`` for production.
        """
        if not symbols:
            raise ValueError("symbols must not be empty")
        key = idempotency_key or f"kit-{uuid.uuid4().hex}"
        headers = {"Idempotency-Key": key}
        if scenario:
            headers["X-Mock-Scenario"] = scenario
        payload = {"symbols": list(symbols), "last_n": last_n,
                   "refresh": bool(refresh)}
        if forms_by_market is not None:
            payload["forms_by_market"] = forms_by_market
        status, resp_headers, raw = self._request(
            "POST", "/api/v1/fetch-jobs", headers=headers, body=payload)
        if status >= 400:
            self._raise_problem(status, resp_headers, raw)
        return status, json.loads(raw.decode("utf-8"))

    def get_job(self, job_id: str, *, timeout: float | None = None) -> dict:
        return self._json("GET", f"/api/v1/fetch-jobs/{job_id}",
                          timeout=timeout)

    def wait_for_terminal(self, job_id: str, *, timeout: float | None = None,
                          poll_interval: float | None = None,
                          on_poll=None) -> dict:
        """Poll until a terminal status or the budget is exhausted.

        ``timeout`` is an overall wall-clock budget in seconds. The budget is
        enforced **during** HTTP I/O, not just between polls:

        * the remaining budget is checked before every request and the request
          socket timeout is capped to it, so a slow response cannot run past
          the budget;
        * the deadline is re-checked after the response before a terminal
          result is accepted as in-budget;
        * a transport timeout is re-raised as :class:`ClientTimeout` carrying
          the job id and the last known document.
        """
        budget = self.max_wait if timeout is None else timeout
        interval = self.poll_interval if poll_interval is None else poll_interval
        deadline = time.monotonic() + budget
        last = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientTimeout(job_id, last)
            request_timeout = min(self.timeout, remaining) \
                if self.timeout else remaining
            try:
                last = self.get_job(job_id, timeout=request_timeout)
            except ClientTimeout as exc:
                raise ClientTimeout(job_id, last) from exc
            if on_poll is not None:
                on_poll(last)
            if last.get("status") in TERMINAL_STATUSES:
                if time.monotonic() <= deadline:
                    return last
                raise ClientTimeout(job_id, last)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientTimeout(job_id, last)
            time.sleep(min(interval, remaining))

    # -- reports ----------------------------------------------------------- #

    def list_reports(self, *, market=None, symbol=None, doc_type=None,
                     period_from=None, period_to=None, limit=20,
                     cursor=None) -> dict:
        params = {}
        for name, value in (("market", market), ("symbol", symbol),
                            ("doc_type", doc_type),
                            ("period_from", period_from),
                            ("period_to", period_to),
                            ("limit", limit), ("cursor", cursor)):
            if value is not None:
                params[name] = value
        query = urlencode(params)
        path = "/api/v1/reports" + (f"?{query}" if query else "")
        return self._json("GET", path)

    def get_report(self, report_id: str) -> dict:
        return self._json("GET", f"/api/v1/reports/{report_id}")

    def download_report_file(self, report_id: str, artifact_id=None, *,
                             if_none_match=None, timeout=None) -> dict:
        path = f"/api/v1/reports/{report_id}/file"
        if artifact_id is not None:
            path += "?" + urlencode({"artifact_id": artifact_id})
        status, headers, raw = self._request(
            "GET", path, headers={"Accept": "*/*"},
            if_none_match=if_none_match, timeout=timeout)
        if status == 304:
            return {"status": 304, "content": None,
                    "etag": _header(headers, "ETag")}
        if status >= 400:
            self._raise_problem(status, headers, raw)
        return {
            "status": status,
            "content": raw,
            "sha256": sha256_hex(raw),
            "etag": _header(headers, "ETag"),
            "media_type": _header(headers, "Content-Type"),
            "content_disposition": _header(headers, "Content-Disposition"),
            "content_length": int(_header(headers, "Content-Length")
                                  or len(raw)),
        }

    @staticmethod
    def sha256_hex(data: bytes) -> str:
        return sha256_hex(data)
