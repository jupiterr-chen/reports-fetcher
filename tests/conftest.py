"""测试公共设施：fixture 加载、假 session/limiter、报告构造器。测试不触网。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reports_fetcher.config import Config, HttpConfig
from reports_fetcher.downloader import RateLimiter, Transport
from reports_fetcher.models import (
    DocumentRole,
    Market,
    PeriodSource,
    Report,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

HTML_DOC = (b"<!DOCTYPE html><html><head><title>Form 10-Q</title></head><body>"
            b"<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>"
            + b"x" * 2048 + b"</body></html>")

PDF_DOC = b"%PDF-1.7\n" + b"1 0 obj\n<< /Type /Catalog >>\nendobj\n" * 80 \
           + b"trailer\n<< /Size 4 >>\nstartxref\n123\n%%EOF\n"


def load_fixture(rel: str):
    with open(FIXTURES / rel, encoding="utf-8") as fh:
        return json.load(fh)


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, url=""):
        self.status_code = status_code
        self.content = content
        self.headers = dict(headers or {})
        self.url = url

    def iter_content(self, chunk_size=8192):
        view = memoryview(self.content)
        for i in range(0, len(view), chunk_size):
            yield bytes(view[i:i + chunk_size])

    def json(self):
        return json.loads(self.content)


class FakeSession:
    """脚本化 session：按序弹出响应（响应对象 / 异常 / 可调用）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(method, url, **kwargs)
        return item

    def mount(self, *args, **kwargs):
        pass

    def close(self):
        pass


class UrlMapSession:
    """按 URL 映射响应的 session（适配器测试）。"""

    def __init__(self, mapping: dict):
        self.mapping = mapping
        self.requests: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        handler = self.mapping[url]
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(method, url, **kwargs)
        return handler

    def mount(self, *args, **kwargs):
        pass

    def close(self):
        pass


class CountingLimiter(RateLimiter):
    """记录每次 wait 的来源组（不实际等待）——验证"每次尝试都过限速器"。"""

    def __init__(self) -> None:
        super().__init__({})
        self.waits: list[str] = []

    def wait(self, group: str) -> float:
        self.waits.append(group)
        return 0.0


class RecordingClock:
    """可手动推进的单调时钟。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_transport(session, *, config: HttpConfig | None = None,
                   limiter: RateLimiter | None = None,
                   sleeps: list[float] | None = None,
                   clock=None) -> Transport:
    def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return Transport(config or HttpConfig(), session=session,
                     clock=clock or (lambda: 0.0), sleep=sleep,
                     limiter=limiter or CountingLimiter())


def make_config(**http_overrides) -> Config:
    config = Config()
    for key, value in http_overrides.items():
        setattr(config.http, key, value)
    return config


def make_report(**overrides) -> Report:
    defaults = dict(
        market=Market.US,
        symbol="AAPL",
        source_id="0000320193-26-000020/aapl-20260627.htm",
        source_url=("https://www.sec.gov/Archives/edgar/data/320193/"
                    "000032019326000020/aapl-20260627.htm"),
        title="aapl-20260627.htm",
        doc_type="10-Q",
        source_form="10-Q",
        filing_date="2026-07-31",
        report_period="2026-06-27",
        period_source=PeriodSource.SOURCE_FIELD,
        language="en",
        document_role=DocumentRole.FULL_REPORT,
    )
    defaults.update(overrides)
    return Report(**defaults)


@pytest.fixture
def us_fixture_paths():
    return {
        "tickers": FIXTURES / "us" / "company_tickers_sample.json",
        "recent": FIXTURES / "us" / "submissions_aapl_recent.json",
        "histfile": FIXTURES / "us" / "submissions_aapl_histfile.json",
        "notfound": FIXTURES / "us" / "submissions_notfound.json",
    }
