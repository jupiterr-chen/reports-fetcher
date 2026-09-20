"""统一传输层与下载器（DESIGN §10，FR-4，NFR-1/2/3）。

所有源站请求（resolve、列表、文件、重试、重定向）都经过本模块：
- 按来源组（而非单 host）限速，线程安全的最小间隔器；
- 显式重试循环，每次重试（含重定向的每一跳）都过限速器；
  requests/urllib3 隐式自动重试显式关闭；
- 流式下载：边写唯一临时文件边累计字节与 SHA-256，缺 Content-Length 也执行上限；
- PDF/HTML 内容联合校验：200/Content-Type/magic 都不能单独证明有效；
- 手动跟随重定向并校验官方域名、HTTPS 与非私网地址。
"""
from __future__ import annotations

import email.utils
import hashlib
import ipaddress
import logging
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from reports_fetcher.config import HttpConfig
from reports_fetcher.models import (
    DownloadedFile,
    DownloadInvalidError,
    DownloadTooLargeError,
    SourceContractChangedError,
    SourceRateLimitedError,
    SourceUnavailableError,
    UnexpectedStatusError,
)

logger = logging.getLogger("reports_fetcher.transport")

_CHUNK_SIZE = 256 * 1024
_MAX_REDIRECT_HOPS = 5
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0
_METADATA_MAX_BYTES = 64 * 1024 * 1024  # 元数据响应读取上限（company_tickers ~1MiB）
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class SourceGroup:
    name: str
    hosts: frozenset[str]
    https_only: bool


# 来源组包含查询及文件域名（I0 实测备案，DESIGN §6/§7/§8）
SOURCE_GROUPS: dict[str, SourceGroup] = {
    "sec": SourceGroup("sec", frozenset({"www.sec.gov", "data.sec.gov"}), True),
    "cninfo": SourceGroup("cninfo", frozenset({"www.cninfo.com.cn",
                                               "static.cninfo.com.cn"}), False),
    "hkex": SourceGroup("hkex", frozenset({"www1.hkexnews.hk",
                                           "www.hkexnews.hk"}), True),
}


def group_for_host(host: str, groups: dict[str, SourceGroup] | None = None) -> SourceGroup:
    """按 host 反查来源组；未备案域名不属于任何预算组，拒绝请求。"""
    groups = groups or SOURCE_GROUPS
    host = (host or "").lower()
    for group in groups.values():
        if host in group.hosts:
            return group
    raise SourceUnavailableError(
        "目标域名不在已备案来源组内", detail=f"host={host}")


class RateLimiter:
    """线程安全的单调时钟最小间隔器（非令牌桶，不做吞吐承诺）。"""

    def __init__(self, intervals: dict[str, float], *, clock=time.monotonic,
                 sleep=time.sleep) -> None:
        self._intervals = dict(intervals)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_ok: dict[str, float] = {}

    def interval(self, group: str) -> float:
        return self._intervals.get(group, 0.0)

    def wait(self, group: str) -> float:
        """阻塞直到该组允许下一次请求；返回实际等待的秒数。"""
        interval = self._intervals.get(group, 0.0)
        if interval <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            earliest = self._next_ok.get(group, now)
            wait_for = max(0.0, earliest - now)
            self._next_ok[group] = max(now, earliest) + interval
        if wait_for > 0:
            self._sleep(wait_for)
        return wait_for


class Transport:
    """所有外部 HTTP 的唯一入口。测试可注入 session / clock / sleep / limiter。"""

    def __init__(self, config: HttpConfig, *, session=None,
                 clock=time.monotonic, sleep=time.sleep,
                 limiter: RateLimiter | None = None,
                 groups: dict[str, SourceGroup] | None = None) -> None:
        self.config = config
        self._session = session if session is not None else requests.Session()
        # 显式关闭 requests/urllib3 隐式重试（DESIGN §10.1）
        try:
            adapter = requests.adapters.HTTPAdapter(max_retries=0)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        except (AttributeError, TypeError):
            pass  # 测试注入的假 session 无需挂载
        self._clock = clock
        self._sleep = sleep
        self._limiter = limiter or RateLimiter(config.source_intervals,
                                                clock=clock, sleep=sleep)
        self.groups = groups or SOURCE_GROUPS

    # ---------------------------------------------------------- 元数据请求

    def get_json(self, url: str, *, group: str, headers: dict | None = None):
        """GET 并解析 JSON；200 但非 JSON 视为契约变化，不伪装成空结果。"""
        response = self.request("GET", url, group=group, headers=headers)
        content_type = response.headers.get("Content-Type", "")
        body = self._read_metadata_body(response)
        try:
            import json
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise SourceContractChangedError(
                "响应不是合法 JSON（契约变化或错误页）",
                detail=f"url={url} content_type={content_type!r} "
                       f"body_head={body[:120]!r}") from e

    def post_form_json(self, url: str, *, group: str, data: dict,
                       headers: dict | None = None):
        """已知只读查询 POST（如巨潮列表），可安全重试。"""
        response = self.request("POST", url, group=group, data=data, headers=headers)
        body = self._read_metadata_body(response)
        try:
            import json
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise SourceContractChangedError(
                "响应不是合法 JSON（契约变化或错误页）",
                detail=f"url={url} body_head={body[:120]!r}") from e

    def _read_metadata_body(self, response) -> bytes:
        body = response.content or b""
        if len(body) > _METADATA_MAX_BYTES:
            raise SourceUnavailableError(
                f"元数据响应超过读取上限（{_METADATA_MAX_BYTES} 字节）")
        return body

    # ---------------------------------------------------------- 请求与重试

    def request(self, method: str, url: str, *, group: str,
                data: dict | None = None, headers: dict | None = None,
                stream: bool = False):
        """带显式重试循环的请求；每次尝试（含重定向每一跳）都过限速器。

        返回最终 2xx 响应；非重试错误抛 UnexpectedStatusError（保留状态码
        与响应体开头，由适配器解释语义），网络/限流类错误抛对应来源错误。
        """
        max_retries = self.config.max_retries
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                self._sleep(self._backoff_delay(attempt, last_error))
            self._limiter.wait(group)
            try:
                response = self._issue(method, url, data=data, headers=headers,
                                       stream=stream)
            except requests.RequestException as e:
                last_error = e
                if attempt < max_retries:
                    logger.debug("网络错误（第 %d 次尝试，将重试）: %s %s: %r",
                                 attempt + 1, method, url, e)
                    continue
                raise SourceUnavailableError(
                    f"网络错误（已重试 {max_retries} 次）: {e}",
                    detail=f"url={url}") from e
            except (SourceRateLimitedError, SourceUnavailableError) as e:
                if not getattr(e, "retryable", False):
                    raise  # 校验失败/不可重试诊断错误（如 403）
                last_error = e
                if attempt < max_retries:
                    logger.debug("来源 %s（第 %d 次尝试，将重试）: %s",
                                 type(e).__name__, attempt + 1, e)
                    continue
                raise
            return response
        raise SourceUnavailableError("请求失败", detail=f"url={url}")  # pragma: no cover

    def _issue(self, method: str, url: str, *, data=None, headers=None, stream=False):
        """单次请求 + 手动重定向跟随（每跳过限速器并校验目标）。"""
        current_url = url
        for _hop in range(_MAX_REDIRECT_HOPS + 1):
            # allow_redirects=False：重定向由本方法显式处理
            response = self._session.request(
                method, current_url, data=data, headers=headers,
                timeout=(self.config.connect_timeout_seconds,
                         self.config.read_timeout_seconds),
                stream=stream, allow_redirects=False)
            if response.status_code not in _REDIRECT_STATUS:
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code in _RETRYABLE_STATUS:
                    # 429/5xx 交由上层重试循环统一处理（含 Retry-After 退避）
                    self._retryable_response_error(response, current_url)
                raise UnexpectedStatusError(
                    f"来源返回 HTTP {response.status_code}",
                    status=response.status_code,
                    content_type=response.headers.get("Content-Type", ""),
                    body_head=self._body_head(response))
            location = response.headers.get("Location", "")
            if not location:
                raise SourceUnavailableError(
                    "重定向响应缺少 Location", detail=f"url={current_url}")
            next_url = urllib.parse.urljoin(current_url, location)
            group = self._group_of_url(next_url)
            self._limiter.wait(group.name)
            self._validate_redirect_target(next_url, group)
            current_url = next_url
        raise SourceUnavailableError(
            f"重定向跳数超过上限（{_MAX_REDIRECT_HOPS}）", detail=f"url={url}")

    def _retryable_response_error(self, response, url: str) -> None:
        """构造可重试错误（带 retryable 标记与 Retry-After），供退避计算使用。"""
        retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
        if response.status_code == 429:
            err = SourceRateLimitedError(
                "来源限流（HTTP 429）", detail=f"url={url} retry_after={retry_after}")
        else:
            err = SourceUnavailableError(
                f"来源错误（HTTP {response.status_code}）",
                detail=f"url={url} retry_after={retry_after}")
        err.retry_after = retry_after  # type: ignore[attr-defined]
        err.retryable = True  # type: ignore[attr-defined]
        raise err

    def _backoff_delay(self, attempt: int, last_error: Exception | None) -> float:
        """指数退避 + 抖动；尊重 Retry-After（取两者较大值）。"""
        delay = min(_BACKOFF_CAP_SECONDS,
                    _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        delay = delay / 2 + random.uniform(0, delay / 2)  # equal jitter
        retry_after = getattr(last_error, "retry_after", None)
        if retry_after is not None:
            delay = max(delay, min(float(retry_after), _BACKOFF_CAP_SECONDS * 4))
        return max(0.0, delay)

    def _parse_retry_after(self, value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())

    # ---------------------------------------------------------- 重定向校验

    def _group_of_url(self, url: str) -> SourceGroup:
        parsed = urllib.parse.urlparse(url)
        if not parsed.hostname:
            raise SourceUnavailableError("URL 缺少主机名", detail=f"url={url}")
        return group_for_host(parsed.hostname, self.groups)

    def _validate_redirect_target(self, url: str, group: SourceGroup) -> None:
        """校验每一跳：来源组域名、HTTPS（要求组）、非私网/回环地址。"""
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        scheme = (parsed.scheme or "").lower()
        if group.https_only and scheme != "https":
            raise SourceUnavailableError(
                "来源组要求 HTTPS，重定向目标不合规",
                detail=f"group={group.name} url={url}")
        if scheme not in ("http", "https"):
            raise SourceUnavailableError(
                "不支持的 URL scheme", detail=f"url={url}")
        if host not in group.hosts:
            raise SourceUnavailableError(
                f"重定向目标不在来源组 {group.name} 备案域名内",
                detail=f"host={host}")
        if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
            raise SourceUnavailableError(
                "重定向目标为主机名而非官方域名", detail=f"host={host}")
        # IP 字面量直接做私网检查；DNS 级 SSRF 校验不在一期范围（域名白名单为控制面）
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SourceUnavailableError(
                "重定向目标是私网/回环地址", detail=f"host={host}")

    def _body_head(self, response) -> str:
        try:
            body = response.content or b""
        except Exception:  # pragma: no cover - stream 场景
            return ""
        return body[:400].decode("utf-8", errors="replace")

    @staticmethod
    def _wire_content_length(response) -> int | None:
        """传输表示下的 Content-Length；缺失或非法返回 None。"""
        raw = response.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    # ---------------------------------------------------------- 文件下载

    def download(self, url: str, *, group: str, dest_dir: Path,
                 expected_kind: str = "any",
                 headers: dict | None = None) -> DownloadedFile:
        """流式下载到 dest_dir 下的唯一临时文件并做联合校验。

        下载器只产出已校验临时文件，不提交最终归档、不更新 manifest。
        expected_kind: pdf / html / any（按已验证内容判定，源 URL 后缀仅作提示）。
        """
        import uuid
        dest_dir.mkdir(parents=True, exist_ok=True)
        temp_path = dest_dir / f".tmp-{uuid.uuid4().hex}.part"
        deadline = self._clock() + self.config.file_deadline_seconds
        hasher = hashlib.sha256()
        total = 0

        try:
            response = self.request("GET", url, group=group, headers=headers,
                                    stream=True)
            content_length = self._wire_content_length(response)
            content_encoded = "Content-Encoding" in response.headers
            with open(temp_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.config.max_file_bytes:
                        raise DownloadTooLargeError(
                            "文件超过字节上限",
                            detail=f"limit={self.config.max_file_bytes} "
                                   f"bytes>= {total}")
                    hasher.update(chunk)
                    fh.write(chunk)
                if self._clock() > deadline:
                    raise SourceUnavailableError(
                        "文件下载超过截止时间",
                        detail=f"deadline={self.config.file_deadline_seconds}s url={url}")
            # Content-Length 仅在同一传输表示下比较（DESIGN §10.2）
            if content_length is not None and not content_encoded \
                    and content_length != total:
                raise DownloadInvalidError(
                    "响应体长度与 Content-Length 不一致（疑似截断）",
                    detail=f"content_length={content_length} actual={total}")
            media_type = validate_file(temp_path, expected_kind=expected_kind)
            return DownloadedFile(
                temp_path=str(temp_path), sha256=hasher.hexdigest(), bytes=total,
                media_type=media_type, final_url=response.url or url)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise


# -------------------------------------------------------------- 内容校验

_ERROR_SIGNATURES = [
    b"undeclared automated tool",   # SEC 自动工具拦截页（大小写不敏感匹配）
    b"access denied",
    b"request unsuccessful. incap",
    b"to continue, please type the characters you see below",  # 验证码页
]


def _read_head(path: Path, size: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(size)


def _read_tail(path: Path, size: int) -> bytes:
    with open(path, "rb") as fh:
        try:
            fh.seek(0, 2)
            end = fh.tell()
            fh.seek(max(0, end - size))
            return fh.read(size)
        except OSError:  # pragma: no cover
            return b""


def detect_media_kind(head: bytes) -> str | None:
    """按内容嗅探类型；与 Content-Type 无关。

    SEC 现代主文档为 Inline XBRL：以 XML 声明 + 供应商注释开头，其后才是
    <html>（2026-09-20 实测），故在开头窗口内探测而非仅看首标签。
    """
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"%PDF-"):
        return "pdf"
    window = stripped[:1024].lower()
    if window.startswith(b"<!doctype html") or b"<html" in window:
        return "html"
    return None


def validate_file(path: Path, *, expected_kind: str = "any") -> str:
    """联合校验已下载文件；通过返回规范 media_type，失败抛 download_invalid。

    - PDF：magic %PDF- + 尾部 %%EOF 完整性特征；
    - HTML：文档结构开头 + 收尾结构 + 已知错误/验证页排除；
    - 0 字节、类型不明、与预期不符均视为无效。
    """
    import os
    if not path.is_file() or (size := os.path.getsize(path)) == 0:
        raise DownloadInvalidError("下载内容为空", detail=f"path={path.name}")
    head = _read_head(path, 4096)
    kind = detect_media_kind(head)
    if kind is None:
        raise DownloadInvalidError(
            "无法识别内容类型（非 PDF/HTML）",
            detail=f"path={path.name} head={head[:80]!r}")
    if expected_kind != "any" and kind != expected_kind:
        raise DownloadInvalidError(
            f"内容类型与预期不符：期望 {expected_kind}，实际 {kind}",
            detail=f"path={path.name}")
    head_lower = head.lower()
    tail = _read_tail(path, 4096)
    for signature in _ERROR_SIGNATURES:
        if signature in head_lower or signature in tail.lower():
            raise DownloadInvalidError(
                "命中已知错误/验证页面签名",
                detail=f"path={path.name} signature={signature!r}")
    if kind == "pdf":
        if b"%%eof" not in tail.lower():
            raise DownloadInvalidError(
                "PDF 缺少 %%EOF 完整性特征（疑似截断）",
                detail=f"path={path.name}")
        return "application/pdf"
    # HTML 结构完整性：必须存在收尾/正文结构特征
    if b"</html" not in tail.lower() and b"</body" not in tail.lower() \
            and b"<body" not in head_lower:
        raise DownloadInvalidError(
            "HTML 缺少文档结构特征（疑似错误页/截断）",
            detail=f"path={path.name} size={size}")
    return "text/html"


def expected_kind_for_url(url: str) -> str:
    """源 URL 后缀仅作 kind 提示（DESIGN §11.1），最终以内容校验为准。"""
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(".htm") or path.endswith(".html"):
        return "html"
    return "any"
