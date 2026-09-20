"""downloader：传输层（fake session/clock 驱动，不触网；DESIGN §10 / §16）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import requests

from reports_fetcher.config import HttpConfig
from reports_fetcher.downloader import (
    RateLimiter,
    Transport,
    detect_media_kind,
    expected_kind_for_url,
    validate_file,
)
from reports_fetcher.models import (
    DownloadInvalidError,
    DownloadTooLargeError,
    SourceContractChangedError,
    SourceRateLimitedError,
    SourceUnavailableError,
    UnexpectedStatusError,
)
from tests.conftest import (
    CountingLimiter,
    FakeResponse,
    FakeSession,
    HTML_DOC,
    PDF_DOC,
    RecordingClock,
    make_transport,
)

SEC_URL = "https://www.sec.gov/files/company_tickers.json"


def _ok_json(payload=None, url=SEC_URL):
    body = json.dumps(payload if payload is not None else {"ok": 1}).encode()
    return FakeResponse(200, body, headers={"Content-Type": "application/json"},
                        url=url)


class TestRateLimiter:
    def test_min_interval_enforced(self):
        clock = RecordingClock()
        sleeps: list[float] = []
        limiter = RateLimiter({"sec": 0.13}, clock=clock, sleep=sleeps.append)
        assert limiter.wait("sec") == 0.0      # 首次立即通过
        assert sleeps == []
        assert limiter.wait("sec") == pytest.approx(0.13, abs=1e-9)
        sleeps.clear()
        clock.advance(0.5)                     # 越过下次放行点（1000.26）
        assert limiter.wait("sec") == 0.0

    def test_groups_independent(self):
        clock = RecordingClock()
        sleeps: list[float] = []
        limiter = RateLimiter({"sec": 0.13, "cninfo": 0.5},
                              clock=clock, sleep=sleeps.append)
        limiter.wait("sec")
        limiter.wait("cninfo")
        assert sleeps == []  # 不同组互不阻塞

    def test_interval_zero_never_sleeps(self):
        limiter = RateLimiter({"sec": 0})
        assert limiter.wait("sec") == 0.0
        assert limiter.wait("sec") == 0.0


class TestRetryBudget:
    def test_every_attempt_passes_limiter(self):
        limiter = CountingLimiter()
        session = FakeSession([
            requests.ConnectionError("tls reset"),
            requests.ConnectionError("tls reset"),
            _ok_json(),
        ])
        transport = make_transport(session, limiter=limiter)
        transport.get_json(SEC_URL, group="sec")
        assert len(session.requests) == 3
        assert limiter.waits == ["sec", "sec", "sec"]  # 含重试每次都过限速器

    def test_retry_exhaustion_raises_source_unavailable(self):
        session = FakeSession([requests.ConnectionError("down")] * 4)
        transport = make_transport(session)  # 默认 max_retries=3 → 4 次尝试
        with pytest.raises(SourceUnavailableError):
            transport.get_json(SEC_URL, group="sec")
        assert len(session.requests) == 4

    def test_500_retried_then_success(self):
        sleeps: list[float] = []
        session = FakeSession([
            FakeResponse(500, b"err"),
            FakeResponse(503, b"err"),
            _ok_json(),
        ])
        transport = make_transport(session, sleeps=sleeps)
        transport.get_json(SEC_URL, group="sec")
        assert len(session.requests) == 3
        assert len(sleeps) == 2  # 两次重试前各有退避

    def test_backoff_grows(self):
        sleeps: list[float] = []
        session = FakeSession([FakeResponse(500, b"err")] * 3 + [_ok_json()])
        transport = make_transport(session, sleeps=sleeps)
        transport.get_json(SEC_URL, group="sec")
        assert len(sleeps) == 3
        # equal jitter：delay ∈ [d/2, d]；d 序列 0.5 / 1 / 2
        assert 0.25 <= sleeps[0] <= 0.5
        assert 0.5 <= sleeps[1] <= 1.0
        assert 1.0 <= sleeps[2] <= 2.0

    def test_429_respects_retry_after(self):
        sleeps: list[float] = []
        session = FakeSession([
            FakeResponse(429, b"slow down",
                         headers={"Retry-After": "2"}),
            _ok_json(),
        ])
        transport = make_transport(session, sleeps=sleeps)
        transport.get_json(SEC_URL, group="sec")
        assert sleeps[0] >= 2.0  # Retry-After 优先于更短的指数退避

    def test_429_exhaustion_raises_rate_limited(self):
        session = FakeSession([
            FakeResponse(429, b"x", headers={"Retry-After": "0"})] * 4)
        transport = make_transport(session)
        with pytest.raises(SourceRateLimitedError):
            transport.get_json(SEC_URL, group="sec")

    def test_403_not_retried(self):
        session = FakeSession([FakeResponse(403, b"forbidden")])
        transport = make_transport(session)
        with pytest.raises(UnexpectedStatusError) as ei:
            transport.get_json(SEC_URL, group="sec")
        assert ei.value.status == 403
        assert len(session.requests) == 1  # 403 立即失败，不重试

    def test_404_not_retried_and_body_preserved(self):
        session = FakeSession([FakeResponse(
            404, b"<?xml?><Error><Code>NoSuchKey</Code></Error>",
            headers={"Content-Type": "application/xml"})])
        transport = make_transport(session)
        with pytest.raises(UnexpectedStatusError) as ei:
            transport.get_json("https://data.sec.gov/submissions/CIK0009999999.json",
                               group="sec")
        assert ei.value.status == 404
        assert "NoSuchKey" in ei.value.body_head

    def test_200_non_json_is_contract_change(self):
        session = FakeSession([FakeResponse(
            200, b"<html>maintenance</html>",
            headers={"Content-Type": "text/html"})])
        transport = make_transport(session)
        with pytest.raises(SourceContractChangedError):
            transport.get_json(SEC_URL, group="sec")


class TestRedirects:
    DOCS_URL = "https://www.sec.gov/Archives/edgar/data/320193/doc.htm"

    def test_same_group_redirect_followed(self):
        limiter = CountingLimiter()
        session = FakeSession([
            FakeResponse(302, b"", headers={
                "Location": "https://www.sec.gov/Archives/edgar/data/320193/d2.htm"}),
            FakeResponse(200, HTML_DOC,
                         headers={"Content-Type": "text/html"},
                         url="https://www.sec.gov/Archives/edgar/data/320193/d2.htm"),
        ])
        transport = make_transport(session, limiter=limiter)
        body = transport.request("GET", self.DOCS_URL, group="sec")
        assert body.status_code == 200
        # 初始请求 + 重定向跳 = 2 次过限速器
        assert limiter.waits == ["sec", "sec"]
        assert len(session.requests) == 2

    def test_redirect_to_foreign_host_rejected(self):
        session = FakeSession([
            FakeResponse(302, b"", headers={"Location": "https://evil.example.com/x"}),
        ])
        transport = make_transport(session)
        with pytest.raises(SourceUnavailableError):
            transport.request("GET", self.DOCS_URL, group="sec")
        assert len(session.requests) == 1  # 未向未备案目标发请求

    def test_http_downgrade_rejected_for_https_only_group(self):
        session = FakeSession([
            FakeResponse(302, b"", headers={"Location": "http://www.sec.gov/x"}),
        ])
        transport = make_transport(session)
        with pytest.raises(SourceUnavailableError):
            transport.request("GET", self.DOCS_URL, group="sec")

    def test_private_ip_literal_rejected(self):
        groups = {"t": type("G", (), {"name": "t", "hosts": frozenset({"10.0.0.5"}),
                                       "https_only": False})()}
        session = FakeSession([
            FakeResponse(302, b"", headers={"Location": "http://10.0.0.5/x"})])
        transport = Transport(HttpConfig(), session=session,
                              clock=lambda: 0.0, sleep=lambda s: None,
                              groups=groups)
        with pytest.raises(SourceUnavailableError):
            transport.request("GET", "http://10.0.0.5/start", group="t")

    def test_too_many_hops_rejected(self):
        session = FakeSession([
            FakeResponse(302, b"", headers={
                "Location": "https://www.sec.gov/hop%d" % i})
            for i in range(8)])
        transport = make_transport(session)
        with pytest.raises(SourceUnavailableError):
            transport.request("GET", self.DOCS_URL, group="sec")


class TestDownload:
    def _download(self, tmp_path, content, *, headers=None, config=None,
                  url="https://www.sec.gov/Archives/edgar/data/320193/aapl.htm",
                  expected_kind="any"):
        session = FakeSession([
            FakeResponse(200, content, headers=headers or {}, url=url)])
        transport = make_transport(session, config=config)
        return transport.download(url, group="sec", dest_dir=tmp_path,
                                  expected_kind=expected_kind), transport

    def test_streaming_sha256_and_media_type(self, tmp_path):
        downloaded, _ = self._download(tmp_path, HTML_DOC,
                                       headers={"Content-Type": "text/html"})
        assert downloaded.bytes == len(HTML_DOC)
        assert downloaded.sha256 == hashlib.sha256(HTML_DOC).hexdigest()
        assert downloaded.media_type == "text/html"
        assert Path(downloaded.temp_path).is_file()
        assert downloaded.temp_path.endswith(".part")

    def test_byte_cap_enforced_without_content_length(self, tmp_path):
        config = HttpConfig(max_file_bytes=100)
        with pytest.raises(DownloadTooLargeError):
            self._download(tmp_path, HTML_DOC, config=config)
        assert list(tmp_path.glob("*.part")) == []  # 失败临时文件已清理

    def test_content_length_mismatch_is_invalid(self, tmp_path):
        with pytest.raises(DownloadInvalidError):
            self._download(tmp_path, HTML_DOC,
                           headers={"Content-Length": str(len(HTML_DOC) + 10)})

    def test_content_length_ignored_when_encoded(self, tmp_path):
        # Content-Encoding 存在时不比较解压长度与压缩长度（DESIGN §10.2）
        downloaded, _ = self._download(
            tmp_path, HTML_DOC,
            headers={"Content-Length": "3", "Content-Encoding": "gzip"})
        assert downloaded.bytes == len(HTML_DOC)

    def test_expected_kind_mismatch(self, tmp_path):
        with pytest.raises(DownloadInvalidError):
            self._download(tmp_path, HTML_DOC, expected_kind="pdf")

    def test_file_deadline(self, tmp_path):
        clock = RecordingClock()

        class AdvancingSession(FakeSession):
            def request(self, method, url, **kwargs):
                clock.advance(10_000)  # 每次请求后时钟大幅前进
                return super().request(method, url, **kwargs)

        session = AdvancingSession(
            [FakeResponse(200, HTML_DOC, headers={}, url="x")])
        transport = Transport(HttpConfig(file_deadline_seconds=600),
                              session=session, clock=clock,
                              sleep=lambda s: None, limiter=CountingLimiter())
        with pytest.raises(SourceUnavailableError):
            transport.download("https://www.sec.gov/Archives/edgar/data/1/a.htm",
                               group="sec", dest_dir=tmp_path)


class TestContentValidation:
    def test_pdf_magic_and_eof(self):
        assert detect_media_kind(PDF_DOC[:64]) == "pdf"

    def test_inline_xbrl_document_detected_as_html(self):
        # SEC 现代主文档实测形态：XML 声明 + Workiva 注释开头（2026-09-20）
        head = (b"<?xml version='1.0' encoding='ASCII'?>\n"
                b"<!--XBRL Document Created with Workiva -->\n"
                b"<html xmlns=\"http://www.w3.org/1999/xhtml\">")
        assert detect_media_kind(head) == "html"

    def test_plain_html_detected(self):
        assert detect_media_kind(b"<!DOCTYPE html>\n<html>") == "html"
        assert detect_media_kind(b"\xef\xbb\xbf<html lang=\"en\">") == "html"

    def test_unknown_content_not_detected(self):
        assert detect_media_kind(b"PD\x00binary junk") is None
        assert detect_media_kind(b"<?xml version='1.0'?><data><x/></data>") is None

    def test_pdf_missing_eof_rejected(self, tmp_path):
        truncated = PDF_DOC.rstrip(b"%%EOF\n") + b"garbage"
        path = tmp_path / "t.pdf"
        path.write_bytes(truncated)
        with pytest.raises(DownloadInvalidError):
            validate_file(path)

    def test_html_structure_required(self, tmp_path):
        path = tmp_path / "t.html"
        path.write_bytes(b"<p>fragment only, no document structure")
        with pytest.raises(DownloadInvalidError):
            validate_file(path)

    def test_sec_block_page_rejected(self, tmp_path):
        page = (b"<!DOCTYPE html><html><head><title>NO ACCESS</title></head>"
                b"<body>Your Request Originates from an Undeclared Automated "
                b"Tool.</body></html>")
        path = tmp_path / "t.html"
        path.write_bytes(page)
        with pytest.raises(DownloadInvalidError):
            validate_file(path)

    def test_empty_file_rejected(self, tmp_path):
        path = tmp_path / "t.html"
        path.write_bytes(b"")
        with pytest.raises(DownloadInvalidError):
            validate_file(path)

    def test_expected_kind_for_url_hint(self):
        assert expected_kind_for_url("https://x/a.pdf") == "pdf"
        assert expected_kind_for_url("https://x/a.htm") == "html"
        assert expected_kind_for_url("https://x/a.HTML") == "html"
        assert expected_kind_for_url("https://x/a.txt") == "any"
