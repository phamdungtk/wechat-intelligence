"""Integration wrapper for the provider-based WeChat article downloader.

The upstream project is kept under ``vendor/`` so the web app can use its
tested history provider and full HTML/asset exporter without asking users to
paste short-lived credentials into the UI.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import time
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent.parent
CAPTURE_LOG_PATH = BASE_DIR / "logs" / "wechat_capture.log"
CAPTURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
capture_logger = logging.getLogger("wechat.capture")
if not capture_logger.handlers:
    capture_handler = RotatingFileHandler(
        CAPTURE_LOG_PATH,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    capture_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    capture_logger.addHandler(capture_handler)
    capture_logger.setLevel(logging.INFO)
    capture_logger.propagate = False
VENDOR_SCRIPTS = (
    BASE_DIR
    / "vendor"
    / "wechat-article-downloader-skill"
    / "skills"
    / "wechat-article-downloader"
    / "scripts"
)
if str(VENDOR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(VENDOR_SCRIPTS))

from wechat_article_downloader.capture import (  # type: ignore  # noqa: E402
    build_account_home_url,
    default_wechat_roots,
    scan_credentials,
)
from wechat_article_downloader.client import WeChatClient  # type: ignore  # noqa: E402
from wechat_article_downloader.errors import AuthRequiredError, WeChatDownloaderError  # type: ignore  # noqa: E402
from wechat_article_downloader.exporter import ArticleExporter  # type: ignore  # noqa: E402
from wechat_article_downloader.provider import WeChatHistoryProvider  # type: ignore  # noqa: E402
from wechat_article_downloader.service import DownloadService  # type: ignore  # noqa: E402
from wechat_article_downloader.utils import parse_iso_date  # type: ignore  # noqa: E402


class WeChatAccountDownloader:
    """Small application-facing facade around the upstream service."""

    def __init__(self, base_dir: Path = BASE_DIR) -> None:
        self.base_dir = base_dir
        self.data_dir = base_dir / "storage" / "wechat_account"
        self.output_dir = base_dir / "storage" / "wechat_articles"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.service = DownloadService(data_dir=self.data_dir)

    @staticmethod
    def extract_biz(source: str) -> str | None:
        """Extract the public-account identifier from article/profile URLs."""
        try:
            query = parse_qs(urlparse(source).query)
        except ValueError:
            return None
        values = query.get("__biz") or query.get("biz")
        return values[0] if values else None

    @staticmethod
    def is_profile_url(source: str) -> bool:
        try:
            parsed = urlparse(source)
        except ValueError:
            return False
        return parsed.netloc.endswith("mp.weixin.qq.com") and (
            "profile_ext" in parsed.path or parsed.query.find("__biz=") >= 0
        )

    def guide(self, source: str, alias: str = "default") -> dict[str, object]:
        if self.is_profile_url(source):
            biz = self.extract_biz(source)
            if not biz:
                raise ValueError("URL profile_ext chưa có __biz.")
            return {
                "ok": True,
                "biz": biz,
                "account_name": None,
                "home_url": build_account_home_url(biz),
                "alias": alias,
                "action": "Mở home_url trong WeChat Desktop đã đăng nhập, sau đó bấm Bắt đầu quét phiên.",
            }
        return self.service.auth_guide(source, alias)

    def capture(
        self,
        source: str,
        *,
        alias: str = "default",
        watch_seconds: int = 30,
        # Weixin often flushes the request cache lazily, so a request made a
        # few minutes ago can still have the original file mtime. Keep a
        # three-hour window and let provider validation reject expired tokens.
        since_minutes: int = 180,
        include_temp: bool = False,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Capture and validate a short-lived login session from local WeChat cache."""
        started_at = time.monotonic()
        capture_logger.info(
            "capture_start biz=%s alias=%s watch_seconds=%s since_minutes=%s include_temp=%s",
            self.extract_biz(source) or "unknown",
            alias,
            watch_seconds,
            since_minutes,
            include_temp,
        )
        if not self.is_profile_url(source):
            capture_logger.info("capture_delegate article_url=true")
            return self.service.auth_capture(
                source,
                alias=alias,
                watch_seconds=watch_seconds,
                since_minutes=since_minutes,
                include_temp=include_temp,
                progress=progress,
            )
        biz = self.extract_biz(source)
        if not biz:
            raise ValueError("Không đọc được __biz từ URL Official Account.")
        config = self.service.config_store.load()
        deadline = time.monotonic() + watch_seconds
        attempted: set[tuple[str, ...]] = set()
        seen_versions: set[tuple[str, int, int]] = set()
        last_report: dict[str, object] | None = None
        last_error = ""
        roots = default_wechat_roots(include_temp=include_temp)
        capture_logger.info("capture_roots roots=%s", [str(root) for root in roots])
        while time.monotonic() <= deadline:
            report = scan_credentials(
                biz,
                since_minutes=since_minutes,
                seen_versions=seen_versions,
                roots=roots,
                # A fresh Weixin request is normally written to the newest
                # cache files. Keep each pass bounded so the button feels
                # interactive even when Temp contains many unrelated files.
                max_files=1200,
                max_scan_seconds=5.0,
            )
            last_report = report.summary()
            capture_logger.info(
                "capture_scan files=%s bytes=%s candidates=%s duration=%.3fs truncated=%s reasons=%s",
                report.scanned_files,
                report.scanned_bytes,
                len(report.candidates),
                report.duration_seconds,
                report.truncated,
                report.truncation_reasons or [],
            )
            if progress:
                progress(
                    {
                        "phase": "scan",
                        "home_url": build_account_home_url(biz),
                        "candidate_count": len(report.candidates),
                        "scanned_files": report.scanned_files,
                        "scanned_bytes": report.scanned_bytes,
                    }
                )
            # Candidates are newest-first. A new desktop request should be
            # among the first few; trying every historical token makes the UI
            # look hung because each validation is rate-limited.
            rejected_in_pass = 0
            for candidate in report.candidates[:3]:
                identity = tuple(
                    candidate.fields.get(field, "")
                    for field in ("biz", "uin", "key", "pass_ticket", "appmsg_token", "poc_token")
                )
                if identity in attempted:
                    continue
                attempted.add(identity)
                credential = dict(candidate.fields)
                credential["request_url"] = candidate.request_url
                capture_logger.info(
                    "capture_candidate source=%s modified_at=%.3f age_minutes=%.1f fields=%s",
                    candidate.source_root,
                    candidate.modified_at,
                    max(0.0, (time.time() - candidate.modified_at) / 60.0),
                    sorted(candidate.fields),
                )
                provider = WeChatHistoryProvider(
                    timeout=config.timeout_seconds,
                    interval_seconds=min(config.request_interval_seconds, 0.5),
                )
                try:
                    items, _next, _more = provider.list_page(
                        biz,
                        credential,
                        offset=0,
                        count=1,
                        referer=source,
                    )
                except WeChatDownloaderError as exc:
                    last_error = f"{exc.code}: {exc}"
                    rejected_in_pass += 1
                    capture_logger.warning("capture_candidate_rejected code=%s error=%s", exc.code, str(exc))
                    continue
                if not items:
                    last_error = "REMOTE_ERROR: phiên được chấp nhận nhưng không có bài lịch sử"
                    capture_logger.warning("capture_candidate_empty")
                    continue
                summary = self.service.credentials.save_fields(alias, credential, origin="local-wechat-cache")
                capture_logger.info(
                    "capture_success validated_articles=%s elapsed=%.3fs",
                    len(items),
                    time.monotonic() - started_at,
                )
                return {
                    "ok": True,
                    "credential": summary.to_dict(),
                    "validated_articles": len(items),
                    "home_url": build_account_home_url(biz),
                    "scan": last_report,
                }
            if (
                report.candidates
                and rejected_in_pass == min(3, len(report.candidates))
                and "AUTH_REQUIRED" in last_error
            ):
                capture_logger.error(
                    "capture_auth_rejected candidates_checked=%s last_error=%s",
                    rejected_in_pass,
                    last_error,
                )
                raise AuthRequiredError(
                    "WeChat rejected all recent cached credentials (ret=-3).",
                    details={"last_validation_error": last_error, "last_scan": last_report},
                )
            time.sleep(1.0)
        capture_logger.error(
            "capture_timeout elapsed=%.3fs last_error=%s last_scan=%s",
            time.monotonic() - started_at,
            last_error or "none",
            last_report or {},
        )
        raise AuthRequiredError(
            "Không tìm thấy phiên WeChat hợp lệ trong thời gian quét.",
            details={
                "home_url": build_account_home_url(biz),
                "action": "Giữ Weixin đăng nhập, mở URL Official Account trong WeChat rồi thử lại.",
                "last_validation_error": last_error or None,
                "last_scan": last_report,
            },
        )

    def download_account(
        self,
        source: str,
        *,
        alias: str = "default",
        latest: int | None = None,
        on_date: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, object]:
        """Download one account selection; accepts either article or profile_ext URL."""
        if not self.is_profile_url(source):
            return self.service.download_account(
                source,
                credential_alias=alias,
                latest=latest,
                on_date=on_date,
                since=since,
                until=until,
                formats="html,md,json",
                output_dir=self.output_dir,
                include_assets=True,
            )

        exact_date = parse_iso_date(on_date, "date")
        start_date = parse_iso_date(since, "since")
        end_date = parse_iso_date(until, "until")
        if exact_date:
            start_date = end_date = exact_date
        if latest is None and start_date is None and end_date is None:
            raise ValueError("Cần chọn latest, date, since hoặc until.")
        if latest is not None and not 1 <= latest <= 100:
            raise ValueError("latest phải nằm trong khoảng 1-100.")
        biz = self.extract_biz(source)
        if not biz:
            raise ValueError("Không đọc được __biz từ URL Official Account.")
        config = self.service.config_store.load()
        credential = self.service.credentials.get(alias)
        provider = WeChatHistoryProvider(
            timeout=config.timeout_seconds,
            interval_seconds=config.request_interval_seconds,
        )
        discovered = self.service._discover(  # noqa: SLF001 - upstream service owns selection rules
            provider,
            biz,
            credential,
            sample_url=source,
            latest=latest,
            start_date=start_date,
            end_date=end_date,
            max_pages=config.max_pages,
        )
        exporter = ArticleExporter()
        results: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        client = WeChatClient(timeout=config.timeout_seconds, interval_seconds=config.request_interval_seconds)
        for item in discovered:
            try:
                article = client.fetch_article(item.url)
                client.sanitize_media_urls(article)
                directory = exporter.prepare_directory(article, self.output_dir)
                client.download_assets(article, directory)
                exported = exporter.export(article, directory, ["html", "md", "json"])
                results.append(exported.to_dict())
            except WeChatDownloaderError as exc:
                failures.append({"url": item.url, "title": item.title, "error": str(exc), "code": exc.code})
        return {
            "ok": not failures and discovered.complete,
            "kind": "account",
            "account_biz": biz,
            "selection": {"latest": latest, "date": on_date, "since": since, "until": until},
            "discovered": [item.to_dict() for item in discovered],
            "history_scan": discovered.summary(),
            "downloads": results,
            "failures": failures,
        }
