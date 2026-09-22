"""Contract tests that exercise the REAL mock HTTP endpoint over the network.

Run inside the kit container::

    docker compose -f integration-kit/compose.yaml run --rm test

They never import the mock implementation or the main project: only the
portable client and raw ``urllib`` are used, so passing tests prove the wire
contract, not an internal function.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from client.reports_client import (
    ClientTimeout,
    ProblemError,
    ReportsClient,
    sha256_hex,
)

BASE = os.environ.get("REPORTS_MOCK_BASE_URL", "http://127.0.0.1:18765")
KIT_ROOT = Path(__file__).resolve().parent.parent
TERMINAL = ("succeeded", "partial", "failed")


def raw(method, path, *, headers=None, json_body=None, raw_body=None,
        content_type=None):
    url = BASE + path
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif raw_body is not None:
        data = raw_body
        if content_type:
            hdrs["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=hdrs,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def parse(body):
    return json.loads(body.decode("utf-8"))


class _DelayedJobHandler(http.server.BaseHTTPRequestHandler):
    """Tiny stdlib server that returns a JobStatusOut after a delay."""

    delay = 0.4
    delay_after = 0
    _count = 0

    def log_message(self, *args):
        pass

    def _doc(self, status):
        return {
            "job_id": "job_local", "status": status, "attempt": 1,
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "started_at": None, "finished_at": None, "deadline": None,
            "progress": {"symbols_total": 1, "symbols_finished": 0},
            "summary": {"downloaded": 0, "cached": 0, "failed": 0},
            "results": [],
        }

    def do_GET(self):
        cls = type(self)
        cls._count += 1
        slow = cls._count > self.delay_after
        if slow:
            time.sleep(self.delay)
        body = json.dumps(
            self._doc("succeeded" if slow else "running")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_local_server(delay=0.4, delay_after=0):
    handler = type("DelayedHandler", (_DelayedJobHandler,),
                   {"delay": delay, "delay_after": delay_after, "_count": 0})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class KitTestCase(unittest.TestCase):
    def setUp(self):
        raw("POST", "/__mock/reset")
        self.client = ReportsClient(BASE, poll_interval=0.05, timeout=5.0,
                                    max_wait=30.0)

    # ------------------------------------------------------------------ #
    # health + docs
    # ------------------------------------------------------------------ #

    def test_health_live_ready(self):
        for path in ("/health/live", "/health/ready"):
            status, headers, body = raw("GET", path)
            self.assertEqual(status, 200, body)
            self.assertEqual(parse(body)["status"], "ok")

    def test_docs_and_openapi_offline(self):
        status, headers, body = raw("GET", "/docs")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("/api/v1/fetch-jobs", html)
        self.assertNotIn("cdn.", html.lower())
        self.assertNotIn("unpkg", html.lower())
        self.assertNotIn("jsdelivr", html.lower())

        status, headers, body = raw("GET", "/openapi.json")
        self.assertEqual(status, 200)
        spec = parse(body)
        self.assertEqual(spec["openapi"].split(".")[0], "3")

    # ------------------------------------------------------------------ #
    # happy path
    # ------------------------------------------------------------------ #

    def test_happy_path_download_and_checksum(self):
        status, accepted = self.client.submit_job(["AAPL", "600519"],
                                                  last_n=2)
        self.assertEqual(status, 202, accepted)
        self.assertEqual(accepted["status"], "queued")
        self.assertTrue(accepted["status_url"].endswith(accepted["job_id"]))

        status, headers, _ = raw(
            "GET", f"/api/v1/fetch-jobs/{accepted['job_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Retry-After") is None)

        doc = self.client.wait_for_terminal(accepted["job_id"])
        self.assertEqual(doc["status"], "succeeded", doc)
        self.assertEqual(doc["progress"]["symbols_finished"], 2)
        self.assertEqual(doc["summary"]["downloaded"], 4)

        listed = self.client.list_reports(market="US", symbol="AAPL")
        self.assertEqual(len(listed["items"]), 2)
        item = listed["items"][0]
        for field in ("report_id", "market", "symbol", "source_id", "doc_type",
                      "report_period", "period_source", "artifact_id",
                      "sha256", "bytes", "download_url"):
            self.assertIn(field, item)

        blob = self.client.download_report_file(item["report_id"])
        self.assertEqual(blob["status"], 200)
        self.assertEqual(blob["etag"], f'"{item["sha256"]}"')
        self.assertEqual(blob["content_length"], item["bytes"])
        self.assertIn("attachment", blob["content_disposition"])
        self.assertEqual(sha256_hex(blob["content"]), item["sha256"])
        self.assertIn(b"MOCK", blob["content"])

        status, headers, _ = raw(
            "GET", f"/api/v1/reports/{item['report_id']}/file")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

        status, headers, body = raw(
            "GET", f"/api/v1/reports/{item['report_id']}/file",
            headers={"If-None-Match": blob["etag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

    def test_progress_total_from_queued(self):
        """symbols_total is the normalized submitted count before any result."""
        _, accepted = self.client.submit_job(
            ["AAPL", "600519", "aapl"], last_n=1, scenario="slow",
            idempotency_key="prog-1")
        status, _, body = raw(
            "GET", f"/api/v1/fetch-jobs/{accepted['job_id']}")
        self.assertEqual(status, 200)
        doc = parse(body)
        self.assertIn(doc["status"], ("queued", "running"), doc["status"])
        # AAPL/aapl collapse to one; 600519 stays -> 2 normalized symbols
        self.assertEqual(doc["progress"]["symbols_total"], 2)
        self.assertEqual(doc["progress"]["symbols_finished"], 0)

    def test_download_disposition_readable(self):
        _, accepted = self.client.submit_job(
            ["AAPL"], last_n=1, idempotency_key="disp-1")
        self.client.wait_for_terminal(accepted["job_id"])
        item = self.client.list_reports(market="US", symbol="AAPL")["items"][0]
        blob = self.client.download_report_file(item["report_id"])
        disposition = blob["content_disposition"]
        for token in (item["market"], item["symbol"], item["doc_type"],
                      item["report_period"], item["report_id"]):
            self.assertIn(token, disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn(".html", disposition)

    def test_job_progression_queued_running_terminal(self):
        _, accepted = self.client.submit_job(["AAPL"], last_n=1)
        seen = []
        doc = self.client.wait_for_terminal(
            accepted["job_id"], on_poll=lambda d: seen.append(d["status"]))
        self.assertIn("queued", seen, seen)
        self.assertIn("running", seen, seen)
        self.assertEqual(seen[-1], doc["status"])
        self.assertIn(doc["status"], TERMINAL)

    def test_repeat_job_uses_cached_reports(self):
        _, first = self.client.submit_job(["AAPL"], last_n=2,
                                          idempotency_key="cache-a")
        doc1 = self.client.wait_for_terminal(first["job_id"])
        _, second = self.client.submit_job(["AAPL"], last_n=2,
                                           idempotency_key="cache-b")
        doc2 = self.client.wait_for_terminal(second["job_id"])
        self.assertEqual(doc1["summary"]["downloaded"], 2)
        self.assertEqual(doc2["summary"]["cached"], 2)
        self.assertEqual(doc2["summary"]["downloaded"], 0)
        first_ids = doc1["results"][0]["report_ids"]
        second_ids = doc2["results"][0]["report_ids"]
        self.assertEqual(first_ids, second_ids)
        self.assertTrue(all(i["status"] == "cached"
                            for i in doc2["results"][0]["items"]))

    # ------------------------------------------------------------------ #
    # scenarios
    # ------------------------------------------------------------------ #

    def test_partial_keeps_usable_file_and_unknown_period(self):
        _, accepted = self.client.submit_job(
            ["AAPL"], last_n=2, scenario="partial")
        doc = self.client.wait_for_terminal(accepted["job_id"])
        self.assertEqual(doc["status"], "partial")
        result = doc["results"][0]
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("报告期未知" in w for w in result["warnings"]),
                        result["warnings"])
        unknown = None
        for report_id in result["report_ids"]:
            detail = self.client.get_report(report_id)
            if detail["report_period"] is None:
                unknown = detail
                break
        self.assertIsNotNone(unknown, "partial must include an unknown period")
        self.assertEqual(unknown["period_source"], "unknown")
        blob = self.client.download_report_file(unknown["report_id"])
        self.assertEqual(sha256_hex(blob["content"]),
                         unknown["artifacts"][0]["sha256"])

    def test_failed_job_is_http_200(self):
        _, accepted = self.client.submit_job(
            ["AAPL"], last_n=1, scenario="failed")
        terminal = self.client.wait_for_terminal(accepted["job_id"])
        self.assertEqual(terminal["status"], "failed")
        status, headers, body = raw(
            "GET", f"/api/v1/fetch-jobs/{accepted['job_id']}")
        self.assertEqual(status, 200)
        doc = parse(body)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["results"][0]["report_ids"], [])
        self.assertTrue(doc["results"][0]["error"]["retryable"])

    def test_no_reports_job_succeeds(self):
        _, accepted = self.client.submit_job(
            ["AAPL"], last_n=1, scenario="no_reports")
        doc = self.client.wait_for_terminal(accepted["job_id"])
        self.assertEqual(doc["status"], "succeeded")
        self.assertEqual(doc["results"][0]["status"], "no_reports")
        self.assertIn("no_matching_reports",
                      doc["results"][0]["warnings"])

    def test_queue_full_429_with_retry_after(self):
        status, headers, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "qf-1", "X-Mock-Scenario": "queue_full"},
            json_body={"symbols": ["AAPL"]})
        self.assertEqual(status, 429)
        self.assertTrue(headers.get("Retry-After"))
        problem = parse(body)
        self.assertEqual(problem["code"], "queue_full")
        self.assertTrue(problem["retryable"])
        self.assertEqual(problem["status"], 429)

    def test_unknown_scenario_rejected(self):
        status, _, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "bad-scn", "X-Mock-Scenario": "nope"},
            json_body={"symbols": ["AAPL"]})
        self.assertEqual(status, 422)
        self.assertEqual(parse(body)["code"], "invalid_request")

    def test_slow_scenario_bounded_client_timeout(self):
        _, accepted = self.client.submit_job(
            ["AAPL"], last_n=1, scenario="slow")
        fast = ReportsClient(BASE, poll_interval=0.2, timeout=5.0,
                             max_wait=1.5)
        start = time.monotonic()
        with self.assertRaises(ClientTimeout):
            fast.wait_for_terminal(accepted["job_id"], timeout=1.5)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.0, f"bounded wait took {elapsed:.2f}s")
        self.assertEqual(self.client.health_live()["status"], "ok")

    def test_scenario_isolation_across_concurrent_jobs(self):
        outcomes = {}
        errors = []

        def run(key, scenario):
            try:
                client = ReportsClient(BASE, poll_interval=0.05, max_wait=30.0)
                _, accepted = client.submit_job(
                    ["AAPL"], last_n=2, scenario=scenario,
                    idempotency_key=key)
                outcomes[scenario] = client.wait_for_terminal(
                    accepted["job_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append((scenario, repr(exc)))

        threads = [threading.Thread(target=run, args=("iso-success", "success")),
                   threading.Thread(target=run, args=("iso-partial", "partial"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(errors, errors)
        self.assertEqual(outcomes["success"]["status"], "succeeded")
        self.assertEqual(outcomes["partial"]["status"], "partial")
        self.assertFalse(outcomes["success"]["results"][0]["warnings"])
        self.assertTrue(outcomes["partial"]["results"][0]["warnings"])

    # ------------------------------------------------------------------ #
    # idempotency + validation
    # ------------------------------------------------------------------ #

    def test_idempotent_replay_and_conflict(self):
        status1, first = self.client.submit_job(
            ["AAPL"], last_n=2, idempotency_key="idem-1")
        status2, replay = self.client.submit_job(
            ["AAPL"], last_n=2, idempotency_key="idem-1")
        self.assertEqual(status1, 202)
        self.assertEqual(status2, 202)
        self.assertEqual(first["job_id"], replay["job_id"])
        self.client.wait_for_terminal(first["job_id"])
        status3, finished = self.client.submit_job(
            ["AAPL"], last_n=2, idempotency_key="idem-1")
        self.assertEqual(status3, 200)
        self.assertEqual(finished["job_id"], first["job_id"])

        status4, _, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "idem-1"},
            json_body={"symbols": ["AAPL"], "last_n": 3})
        self.assertEqual(status4, 409)
        self.assertEqual(parse(body)["code"], "idempotency_conflict")

    def test_idempotency_uses_effective_defaults(self):
        _, first = self.client.submit_job(
            ["AAPL"], last_n=4, idempotency_key="eff-1")
        status, replay = self.client.submit_job(
            ["AAPL"], last_n=4, refresh=False, idempotency_key="eff-1")
        self.assertEqual(status, 202)
        self.assertEqual(first["job_id"], replay["job_id"])

        defaults = {"CN": ["FY", "H1", "Q1", "Q3"],
                    "HK": ["ANNUAL", "INTERIM"],
                    "US": ["10-K", "10-Q", "20-F"]}
        _, omitted = self.client.submit_job(["AAPL"], last_n=1,
                                            idempotency_key="eff-2")
        status, explicit = self.client.submit_job(
            ["AAPL"], last_n=1, forms_by_market=defaults,
            idempotency_key="eff-2")
        self.assertEqual(status, 202)
        self.assertEqual(omitted["job_id"], explicit["job_id"])

    def test_symbol_alias_dedupe_same_key_equivalence(self):
        _, first = self.client.submit_job(["AAPL"], last_n=2,
                                          idempotency_key="dedupe-1")
        status, replay = self.client.submit_job(["AAPL", "aapl"], last_n=2,
                                                idempotency_key="dedupe-1")
        self.assertEqual(status, 202)
        self.assertEqual(first["job_id"], replay["job_id"])
        doc = self.client.wait_for_terminal(first["job_id"])
        self.assertEqual(len(doc["results"]), 1)
        self.assertEqual(doc["results"][0]["symbol"], "AAPL")

    def test_symbol_count_limit_51_rejected(self):
        status, _, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "too-many"},
            json_body={"symbols": [f"S{i}" for i in range(51)]})
        self.assertEqual(status, 422)
        self.assertEqual(parse(body)["code"], "invalid_request")

    def test_single_form_last_n_20_distinct_reports(self):
        _, accepted = self.client.submit_job(
            ["AAPL"], last_n=20, forms_by_market={"US": ["10-K"]},
            idempotency_key="uniq-20")
        doc = self.client.wait_for_terminal(accepted["job_id"])
        report_ids = doc["results"][0]["report_ids"]
        self.assertEqual(len(report_ids), 20)
        self.assertEqual(len(set(report_ids)), 20)
        listed = self.client.list_reports(market="US", symbol="AAPL",
                                          limit=100)
        self.assertEqual(len(listed["items"]), 20)
        periods = [item["report_period"] for item in listed["items"]]
        self.assertEqual(len(set(periods)), 20)

    def test_coverage_matches_documented_fields(self):
        cases = json.loads((KIT_ROOT / "cases.json").read_text("utf-8"))
        documented = set(cases["public_response_contract"]["coverage_fields"])
        self.assertIn("selected", documented)
        self.assertNotIn("returned", documented)
        for scenario, key in (("success", "cov-s"), ("failed", "cov-f"),
                              ("no_reports", "cov-n")):
            _, accepted = self.client.submit_job(
                ["AAPL"], last_n=2, scenario=scenario, idempotency_key=key)
            doc = self.client.wait_for_terminal(accepted["job_id"])
            coverage = doc["results"][0]["coverage"]
            self.assertTrue(set(coverage).issubset(documented), coverage)
            self.assertIn("selected", coverage)
            self.assertNotIn("returned", coverage)

    def test_validation_rejections(self):
        cases = [
            ("POST", "/api/v1/fetch-jobs", {"Idempotency-Key": "v1"},
             {"symbols": []}, 422, "invalid_request"),
            ("POST", "/api/v1/fetch-jobs", {"Idempotency-Key": "v2"},
             {"symbols": ["AAPL"], "last_n": 21}, 422, "invalid_request"),
            ("POST", "/api/v1/fetch-jobs", {"Idempotency-Key": "v3"},
             {"symbols": ["AAPL"], "last_n": 0}, 422, "invalid_request"),
            ("POST", "/api/v1/fetch-jobs", {"Idempotency-Key": "v4"},
             {"symbols": ["AAPL"], "evil": 1}, 422, "invalid_request"),
            ("POST", "/api/v1/fetch-jobs", {"Idempotency-Key": "v5"},
             {"symbols": ["###"]}, 422, "invalid_request"),
            ("POST", "/api/v1/fetch-jobs", {"Idempotency-Key": "v6"},
             {"symbols": ["AAPL"],
              "forms_by_market": {"US": ["6-K"]}}, 422, "invalid_request"),
        ]
        for method, path, headers, body, want_status, want_code in cases:
            status, _, resp = raw(method, path, headers=headers,
                                  json_body=body)
            self.assertEqual(status, want_status, (body, resp))
            self.assertEqual(parse(resp)["code"], want_code, body)

        status, _, body = raw("POST", "/api/v1/fetch-jobs",
                              json_body={"symbols": ["AAPL"]})
        self.assertEqual(status, 400)
        self.assertEqual(parse(body)["code"], "missing_idempotency_key")

        status, _, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "k" * 129},
            json_body={"symbols": ["AAPL"]})
        self.assertEqual(status, 400)
        self.assertEqual(parse(body)["code"], "invalid_idempotency_key")

        status, _, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "media"},
            raw_body=b"symbols=AAPL",
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)
        self.assertEqual(parse(body)["code"], "unsupported_media_type")

        status, _, body = raw(
            "POST", "/api/v1/fetch-jobs",
            headers={"Idempotency-Key": "bad-json"},
            raw_body=b"{not json", content_type="application/json")
        self.assertEqual(status, 422)
        # problem shape
        problem = parse(body)
        self.assertEqual(problem["code"], "invalid_request")
        for field in ("type", "title", "status", "detail", "instance",
                      "code", "request_id", "retryable"):
            self.assertIn(field, problem)

    # ------------------------------------------------------------------ #
    # reports + files
    # ------------------------------------------------------------------ #

    def test_cursor_pagination_no_duplicates(self):
        _, accepted = self.client.submit_job(["AAPL"], last_n=6,
                                             idempotency_key="page-1")
        self.client.wait_for_terminal(accepted["job_id"])
        seen = set()
        cursor = None
        pages = 0
        while True:
            page = self.client.list_reports(symbol="AAPL", limit=2,
                                            cursor=cursor)
            for item in page["items"]:
                self.assertNotIn(item["report_id"], seen)
                seen.add(item["report_id"])
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
            self.assertLess(pages, 10)
        self.assertEqual(len(seen), 6)
        self.assertEqual(pages, 3)

    def test_report_query_validation(self):
        status, _, body = raw("GET", "/api/v1/reports?cursor=!!!")
        self.assertEqual(status, 400)
        self.assertEqual(parse(body)["code"], "invalid_cursor")
        status, _, body = raw("GET", "/api/v1/reports?limit=101")
        self.assertEqual(status, 422)
        status, _, body = raw("GET", "/api/v1/reports?market=XX")
        self.assertEqual(status, 422)
        status, _, body = raw("GET", "/api/v1/reports?period_from=2026/01/01")
        self.assertEqual(status, 422)

    def test_unknown_ids_and_artifact_mismatch(self):
        status, _, body = raw("GET", "/api/v1/fetch-jobs/job_nope")
        self.assertEqual(status, 404)
        self.assertEqual(parse(body)["code"], "not_found")
        status, _, body = raw("GET", "/api/v1/reports/r_nope")
        self.assertEqual(status, 404)
        status, _, body = raw("GET", "/api/v1/reports/r_nope/file")
        self.assertEqual(status, 404)
        status, _, body = raw(
            "GET", "/api/v1/reports/r_nope/file?artifact_id=a_nope")
        self.assertEqual(status, 404)

        _, accepted = self.client.submit_job(["AAPL"], last_n=2,
                                             idempotency_key="art-1")
        self.client.wait_for_terminal(accepted["job_id"])
        items = self.client.list_reports(symbol="AAPL")["items"]
        a, b = items[0], items[1]
        status, _, body = raw(
            "GET", f"/api/v1/reports/{b['report_id']}/file"
                   f"?artifact_id={a['artifact_id']}")
        self.assertEqual(status, 404)

    def test_get_reports_does_not_create_reports(self):
        before = self.client.list_reports()
        self.assertEqual(before["items"], [])
        self.client.list_reports(market="US", symbol="AAPL")
        self.client.list_reports()
        after = self.client.list_reports()
        self.assertEqual(after["items"], [])

    def test_market_and_symbol_filters(self):
        _, accepted = self.client.submit_job(["AAPL", "600519"],
                                             last_n=2,
                                             idempotency_key="filt-1")
        self.client.wait_for_terminal(accepted["job_id"])
        us = self.client.list_reports(market="US")["items"]
        cn = self.client.list_reports(market="CN")["items"]
        self.assertTrue(us) and self.assertTrue(cn)
        self.assertTrue(all(i["market"] == "US" for i in us))
        self.assertTrue(all(i["market"] == "CN" for i in cn))
        self.assertTrue(all(i["symbol"] == "600519" for i in cn))

    # ------------------------------------------------------------------ #
    # openapi + portability
    # ------------------------------------------------------------------ #

    def test_openapi_declares_corrected_contract(self):
        status, _, body = raw("GET", "/openapi.json")
        self.assertEqual(status, 200)
        spec = parse(body)
        paths = spec["paths"]
        submit = paths["/api/v1/fetch-jobs"]["post"]
        header_names = [p["name"] for p in submit.get("parameters", [])]
        self.assertIn("Idempotency-Key", header_names)
        idem = next(p for p in submit["parameters"]
                    if p["name"] == "Idempotency-Key")
        self.assertTrue(idem["required"])
        self.assertEqual(idem["in"], "header")
        for code in ("200", "202", "400", "409", "413", "415", "422", "429"):
            self.assertIn(code, submit["responses"], code)
        self.assertEqual(
            submit["responses"]["202"]["content"]["application/json"]
            ["schema"]["$ref"],
            "#/components/schemas/JobAccepted")

        job_get = paths["/api/v1/fetch-jobs/{job_id}"]["get"]
        self.assertEqual(
            job_get["responses"]["200"]["content"]["application/json"]
            ["schema"]["$ref"],
            "#/components/schemas/JobStatusOut")
        self.assertIn("404", job_get["responses"])

        file_get = paths["/api/v1/reports/{report_id}/file"]["get"]
        self.assertIn("304", file_get["responses"])
        self.assertIn("404", file_get["responses"])
        self.assertIn("409", file_get["responses"])
        media = file_get["responses"]["200"]["content"]
        self.assertTrue("application/pdf" in media or
                        "application/octet-stream" in media)

        schemas = spec["components"]["schemas"]
        for name in ("JobAccepted", "JobStatusOut", "JobSymbolOut",
                     "JobItemOut", "ReportListItem", "ReportListOut",
                     "ReportDetailOut", "ArtifactOut", "Problem"):
            self.assertIn(name, schemas, name)
        required = schemas["JobStatusOut"]["required"]
        for field in ("job_id", "status", "progress", "summary", "results"):
            self.assertIn(field, required)

    def test_cases_json_matches_scenarios(self):
        cases = json.loads((KIT_ROOT / "cases.json").read_text("utf-8"))
        status, _, body = raw("GET", "/__mock/scenarios")
        self.assertEqual(status, 200)
        live = {s["name"] for s in parse(body)["scenarios"]}
        declared = {s["name"] for s in cases["scenarios"]}
        self.assertEqual(live, declared)

    def test_kit_has_no_parent_project_imports(self):
        forbidden = ["reports" + "_fetcher", "from " + "tests",
                     "import " + "tests."]
        for folder in ("client", "mock", "tests"):
            for path in (KIT_ROOT / folder).rglob("*.py"):
                text = path.read_text("utf-8")
                for needle in forbidden:
                    self.assertNotIn(needle, text,
                                     f"{path} references {needle}")

    # ------------------------------------------------------------------ #
    # example client end to end
    # ------------------------------------------------------------------ #

    def _run_example(self, scenario, out_name):
        out = KIT_ROOT / "examples" / out_name
        env = dict(os.environ)
        env.update({
            "REPORTS_API_BASE_URL": BASE,
            "REPORTS_CLIENT_SYMBOLS": "AAPL",
            "REPORTS_CLIENT_LAST_N": "2",
            "REPORTS_CLIENT_TIMEOUT": "30",
            "REPORTS_CLIENT_POLL": "0.2",
            "REPORTS_CLIENT_OUT": str(out),
            "REPORTS_MOCK_SCENARIO": scenario,
        })
        proc = subprocess.run([sys.executable, "-m", "client.example_client"],
                              cwd=str(KIT_ROOT), env=env,
                              capture_output=True, text=True, timeout=90)
        return proc, out

    def test_example_client_success(self):
        proc, out = self._run_example("success", "test_success")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        files = list(out.glob("*"))
        self.assertEqual(len(files), 2, proc.stdout)
        self.assertTrue(any(b"MOCK" in f.read_bytes() for f in files))

    def test_example_client_partial_keeps_files(self):
        proc, out = self._run_example("partial", "test_partial")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        files = list(out.glob("*"))
        self.assertEqual(len(files), 2, proc.stdout)
        self.assertIn("PARTIAL", proc.stdout)


class DeadlineTestCase(unittest.TestCase):
    """The poll budget must be enforced during HTTP I/O, not only between polls."""

    def test_wait_deadline_caps_slow_http(self):
        server = start_local_server(delay=0.4, delay_after=0)
        try:
            port = server.server_address[1]
            client = ReportsClient(f"http://127.0.0.1:{port}",
                                   timeout=30.0, max_wait=30.0)
            start = time.monotonic()
            with self.assertRaises(ClientTimeout):
                client.wait_for_terminal("job_local", timeout=0.05)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.35,
                            f"accepted a slow response past the budget "
                            f"(elapsed={elapsed:.3f}s)")
        finally:
            server.shutdown()
            server.server_close()

    def test_wait_transport_timeout_keeps_last_document(self):
        server = start_local_server(delay=0.4, delay_after=1)
        try:
            port = server.server_address[1]
            client = ReportsClient(f"http://127.0.0.1:{port}",
                                   timeout=30.0, max_wait=30.0,
                                   poll_interval=0.0)
            with self.assertRaises(ClientTimeout) as ctx:
                client.wait_for_terminal("job_local", timeout=0.15)
            last = ctx.exception.last_document
            self.assertIsNotNone(last)
            self.assertEqual(last["status"], "running")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
