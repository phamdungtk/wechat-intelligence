from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import tkinter
from datetime import datetime
from threading import Lock
from urllib.parse import urlparse
from uuid import uuid4
from typing import Callable
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv

from app.downloader.downloader import download_article
from app.reporting import article_report, daily_vehicle_counts
from app.wechat_account import CAPTURE_LOG_PATH, WeChatAccountDownloader, capture_logger
from app.wechat_history import latest_post
from app.jobs import get_job, start_job
from app.vietnamese_report import (
    VietnameseReportError, has_api_key, load_cached_report, save_vietnamese_report,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="WeChat Intelligence")
app.mount("/stored", StaticFiles(directory=BASE_DIR / "storage"), name="stored")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
account_downloader = WeChatAccountDownloader(BASE_DIR)
refresh_lock = Lock()
logger = logging.getLogger("wechat.articles")
Progress = Callable[[str], None]


def _progress(callback: Progress | None, message: str) -> None:
    if callback:
        callback(message)


def _saved_articles() -> list[tuple[Path, dict, Path]]:
    raw_root = BASE_DIR / "storage" / "raw"
    result = []
    for metadata_path in raw_root.glob("*/*/*/*/*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            article_path = metadata_path.with_name("article.md")
            if (
                not isinstance(metadata, dict)
                or metadata.get("title") in (None, "", "Unknown_Title")
                or metadata.get("account_name") in (None, "", "Unknown_Account")
                or not article_path.is_file()
            ):
                continue
            result.append((metadata_path, metadata, article_path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(result, key=lambda item: item[0].stat().st_mtime, reverse=True)


def _article_id(path: Path) -> str:
    return "/".join(path.parent.relative_to(BASE_DIR / "storage" / "raw").parts)


def _saved_report(directory: Path) -> dict:
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    content = (directory / "article.md").read_text(encoding="utf-8")
    cached = load_cached_report(directory, content)
    error_path = directory / "vietnamese_report_error.txt"
    translation_error = error_path.read_text(encoding="utf-8") if error_path.is_file() and not cached else ""
    return article_report(metadata, content, cached, translation_error)


def _translate_saved_article(directory: Path, progress: Progress | None = None, required: bool = False) -> dict:
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    content = (directory / "article.md").read_text(encoding="utf-8")
    cached = load_cached_report(directory, content)
    if cached:
        _progress(progress, "Báo cáo tiếng Việt đã có sẵn cho nội dung này.")
        return article_report(metadata, content, cached)
    source_report = article_report(metadata, content)
    if source_report["analysis"]["status"] != "needs_translation" and not required:
        return source_report
    if not has_api_key():
        message = "Đã lưu bài gốc, nhưng chưa tạo báo cáo tiếng Việt: thiếu OPENAI_API_KEY trên máy chủ."
        (directory / "vietnamese_report_error.txt").write_text(message, encoding="utf-8")
        _progress(progress, message)
        if required:
            raise HTTPException(status_code=503, detail=message)
        return _saved_report(directory)
    _progress(progress, "Đang tạo báo cáo và kết luận tiếng Việt bằng OpenAI API.")
    try:
        translated = save_vietnamese_report(directory, metadata, content)
    except VietnameseReportError as exc:
        message = f"Đã lưu bài gốc, nhưng chưa tạo được báo cáo tiếng Việt: {exc}"
        (directory / "vietnamese_report_error.txt").write_text(message, encoding="utf-8")
        logger.exception("Vietnamese report generation failed")
        _progress(progress, message)
        if required:
            raise HTTPException(status_code=502, detail=message) from exc
        return _saved_report(directory)
    (directory / "vietnamese_report_error.txt").unlink(missing_ok=True)
    report_data = article_report(metadata, content, translated)
    (directory / "analysis.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "report.md").write_text(
        f"# {metadata.get('title', 'Báo cáo')}\n\n## Báo cáo tiếng Việt\n\n{translated['summary']}\n\n## Kết luận\n\n{translated['conclusion']}\n",
        encoding="utf-8",
    )
    _progress(progress, "Đã lưu báo cáo và kết luận tiếng Việt.")
    return report_data


def _saved_article_directory(account: str, year: str, month: str, day: str, stamp: str) -> Path:
    raw_root = (BASE_DIR / "storage" / "raw").resolve()
    directory = (raw_root / account / year / month / day / stamp).resolve()
    if not directory.is_relative_to(raw_root) or len(directory.relative_to(raw_root).parts) != 5:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài viết")
    if not (directory / "metadata.json").is_file() or not (directory / "article.md").is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy bài viết")
    return directory


def _weixin_executable() -> Path | None:
    """Find the Windows Weixin Desktop executable in common install paths."""
    candidates = [
        Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
        Path(r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Tencent" / "Weixin" / "Weixin.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _launch_weixin_desktop() -> bool:
    executable = _weixin_executable()
    if not executable:
        capture_logger.warning("desktop_launch_missing executable_not_found")
        return False
    try:
        subprocess.Popen([str(executable)])
        capture_logger.info("desktop_launch_ok path=%s", executable)
        return True
    except OSError:
        capture_logger.exception("desktop_launch_failed path=%s", executable)
        return False


@app.get("/api/latest-report")
def latest_report():
    articles = _saved_articles()
    if not articles:
        raise HTTPException(status_code=404, detail="Chưa có báo cáo lịch sử")
    metadata_path, metadata, article_path = articles[0]
    return {"id": _article_id(metadata_path), **_saved_report(metadata_path.parent)}


@app.get("/api/export-vehicles/daily")
def export_vehicles_daily():
    return {"items": daily_vehicle_counts(BASE_DIR / "storage" / "raw")}


@app.get("/api/latest-article")
def latest_article():
    articles = _saved_articles()
    if not articles:
        raise HTTPException(status_code=404, detail="Chưa có bài viết lịch sử")
    _, metadata, article_path = articles[0]
    return {"metadata": metadata, "content": article_path.read_text(encoding="utf-8")}


@app.get("/api/reports")
def reports():
    return [
        {
            "id": _article_id(path),
            "title": metadata.get("title", ""),
            "account_name": metadata.get("account_name", ""),
            "published_at": metadata.get("published_at", ""),
            "time": metadata.get("crawled_at", ""),
            "source_url": metadata.get("url", ""),
        }
        for path, metadata, _ in _saved_articles()
    ]


@app.get("/api/reports/{account}/{year}/{month}/{day}/{stamp}")
def report_by_id(account: str, year: str, month: str, day: str, stamp: str):
    directory = _saved_article_directory(account, year, month, day, stamp)
    metadata_path = directory / "metadata.json"
    return {"id": _article_id(metadata_path), **_saved_report(metadata_path.parent)}


@app.delete("/api/reports/{account}/{year}/{month}/{day}/{stamp}")
def delete_report(account: str, year: str, month: str, day: str, stamp: str):
    """Remove one article from reports and dashboard, retaining a server-side recovery copy."""
    with refresh_lock:
        directory = _saved_article_directory(account, year, month, day, stamp)
        article_id = _article_id(directory / "metadata.json")
        trash_root = BASE_DIR / "storage" / "trash"
        trash_root.mkdir(parents=True, exist_ok=True)
        directory.rename(trash_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S%f')}-{uuid4().hex}")
    return {"deleted": True, "id": article_id}


@app.post("/api/reports/{account}/{year}/{month}/{day}/{stamp}/refresh")
def refresh_report(account: str, year: str, month: str, day: str, stamp: str):
    return _refresh_report(account, year, month, day, stamp)


def _refresh_report(account: str, year: str, month: str, day: str, stamp: str, progress: Progress | None = None):
    """Fetch the saved article's original URL and replace this record in place."""
    _progress(progress, "Kiểm tra bài viết đã lưu và link WeChat gốc.")
    with refresh_lock:
        directory = _saved_article_directory(account, year, month, day, stamp)
        metadata_path = directory / "metadata.json"
        old_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        source_url = str(old_metadata.get("url") or "")
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or not re.match(r"^/s(?:/|$)", parsed.path):
            raise HTTPException(status_code=400, detail="Bài viết này không có link WeChat hợp lệ để cập nhật.")

        storage_root = BASE_DIR / "storage"
        with tempfile.TemporaryDirectory(prefix="wechat-refresh-", dir=storage_root) as temporary:
            temporary_root = Path(temporary)
            try:
                _progress(progress, "Đang tải lại nội dung bài viết từ WeChat.")
                exported = account_downloader.service.download_article(
                    source_url,
                    formats="html,md,json",
                    output_dir=temporary_root / "download",
                    include_assets=True,
                )
                source_dir = Path(str(exported["directory"]))
                source_metadata = dict(exported.get("article") or {})
            except Exception as upstream_error:
                logger.warning("Primary refresh downloader failed", exc_info=True)
                _progress(progress, "Trình tải chính gặp lỗi. Đang thử cách tải dự phòng.")
                fallback_root = temporary_root / "fallback" / "raw"
                fallback_root.mkdir(parents=True, exist_ok=True)
                fallback_dir = download_article(source_url, base_storage_dir=str(fallback_root))
                if not fallback_dir:
                    raise HTTPException(status_code=502, detail=f"Không tải lại được bài viết: {upstream_error}") from upstream_error
                source_dir = Path(fallback_dir)
                source_metadata = json.loads((source_dir / "metadata.json").read_text(encoding="utf-8"))

            source_md = source_dir / "article.md"
            if not source_md.is_file() or not source_md.read_text(encoding="utf-8").strip():
                raise HTTPException(status_code=502, detail="Bài viết tải lại không có nội dung.")
            _progress(progress, "Đã tải nội dung. Đang đọc kết luận và số liệu trong bài.")
            prepared = temporary_root / "prepared"
            prepared.mkdir()
            content = source_md.read_text(encoding="utf-8")
            public_prefix = "/stored/" + "/".join(directory.relative_to(BASE_DIR).parts)
            content = re.sub(r"\]\((assets/[^)]+)\)", rf"]({public_prefix}/\1)", content)
            (prepared / "article.md").write_text(content, encoding="utf-8")
            if (source_dir / "article.html").is_file():
                shutil.copy2(source_dir / "article.html", prepared / "article.html")
            if (source_dir / "assets").is_dir():
                shutil.copytree(source_dir / "assets", prepared / "assets")

            metadata = dict(old_metadata)
            metadata.update({
                "title": source_metadata.get("title") or old_metadata.get("title"),
                "account_name": source_metadata.get("account_name") or old_metadata.get("account_name"),
                "author": source_metadata.get("author") or old_metadata.get("author"),
                "published_at": source_metadata.get("published_at") or old_metadata.get("published_at"),
                "image_urls": source_metadata.get("image_urls") or [
                    str(asset.get("remote_url")) for asset in source_metadata.get("assets", [])
                    if isinstance(asset, dict) and asset.get("remote_url")
                ],
                "assets": source_metadata.get("assets") or [],
                "refreshed_at": datetime.now().isoformat(),
                "status": "downloaded",
            })
            report_data = article_report(metadata, content)
            _progress(progress, "Đang lưu nội dung và cập nhật dashboard.")
            (prepared / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            (prepared / "analysis.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
            (prepared / "report.md").write_text(
                f"# {metadata['title']}\n\n## Kết luận từ bài viết\n\n{report_data['analysis']['report']['conclusion']}\n",
                encoding="utf-8",
            )

            version_root = storage_root / "versions" / directory.relative_to(storage_root / "raw")
            version_root.mkdir(parents=True, exist_ok=True)
            previous_version = version_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S%f')}-{uuid4().hex}"
            directory.rename(previous_version)
            try:
                prepared.rename(directory)
            except Exception:
                previous_version.rename(directory)
                raise
            report_data = _translate_saved_article(directory, progress)
            return {"id": _article_id(directory / "metadata.json"), **report_data}


@app.post("/api/reports/{account}/{year}/{month}/{day}/{stamp}/refresh/jobs")
def refresh_report_job(account: str, year: str, month: str, day: str, stamp: str):
    _saved_article_directory(account, year, month, day, stamp)
    return start_job("refresh", lambda progress: _refresh_report(account, year, month, day, stamp, progress))


@app.post("/api/reports/{account}/{year}/{month}/{day}/{stamp}/vietnamese-report/jobs")
def vietnamese_report_job(account: str, year: str, month: str, day: str, stamp: str):
    directory = _saved_article_directory(account, year, month, day, stamp)
    if not has_api_key():
        raise HTTPException(status_code=503, detail="Chưa cấu hình OPENAI_API_KEY trên máy chủ.")
    return start_job("vietnamese_report", lambda progress: _translate_saved_article(directory, progress, required=True))


@app.get("/api/jobs/{job_id}")
def article_job(job_id: str):
    return get_job(job_id)


@app.get("/api/preview/{account}/{year}/{month}/{day}/{stamp}", response_class=HTMLResponse)
def article_preview(account: str, year: str, month: str, day: str, stamp: str):
    article_path = BASE_DIR / "storage" / "raw" / account / year / month / day / stamp / "article.html"
    if not article_path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy bài viết")

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(article_path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup.find_all("script"):
        tag.decompose()
    for tag in soup.find_all("style"):
        tag.decompose()
    for image in soup.find_all("img"):
        for attribute in ("data-src", "data-original", "data-backup-src"):
            if image.get(attribute):
                image["src"] = image[attribute]
                break
        image.attrs.pop("data-src", None)
        image.attrs.pop("data-original", None)
        image.attrs.pop("data-backup-src", None)

    style = soup.new_tag("style")
    style.string = "body{background:#fff!important;color:#1f2937!important;font-family:Arial,sans-serif;max-width:900px;margin:0 auto;padding:24px;line-height:1.7} img{max-width:100%;height:auto;display:block;margin:16px auto} a{color:#2563eb}"
    if soup.head:
        soup.head.append(style)
    else:
        soup.insert(0, style)
    return HTMLResponse(str(soup))


class ArticleRequest(BaseModel):
    url: HttpUrl


class HistoryRequest(BaseModel):
    source: str | dict[str, str]
    target_date: str = ""


class AccountRequest(BaseModel):
    source: str
    account_name: str = "榴莲产业网"
    latest: int | None = 1
    target_date: str = ""
    since: str = ""
    until: str = ""


class AccountAuthRequest(BaseModel):
    source: str
    alias: str = "default"
    watch_seconds: int = 30
    include_temp: bool = False


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/report", response_class=FileResponse)
def report_page():
    return FileResponse(STATIC_DIR / "report.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/articles", response_class=FileResponse)
def articles_page():
    return FileResponse(STATIC_DIR / "articles.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.post("/api/wechat/desktop/open")
def open_wechat_desktop():
    executable = _weixin_executable()
    if not executable:
        raise HTTPException(status_code=404, detail="Không tìm thấy Weixin Desktop")
    subprocess.Popen([str(executable)])
    return {"status": "opened", "message": "Đã mở Weixin Desktop. Hãy chọn bài và Copy link."}


@app.post("/api/wechat/history/latest")
def fetch_latest_history(payload: HistoryRequest):
    target_date = payload.target_date.strip()
    if target_date:
        try:
            from datetime import date
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Ngày cần có định dạng YYYY-MM-DD") from exc
    try:
        post = latest_post(payload.source, target_date or None)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không đọc được lịch sử Weixin: {exc}") from exc
    result = _process_article_url(post["url"])
    result["history_post"] = post
    return result


@app.post("/api/wechat/account/auth/guide")
def account_auth_guide(payload: AccountAuthRequest):
    try:
        return account_downloader.guide(payload.source.strip(), payload.alias)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/wechat/account/auth/capture")
def account_auth_capture(payload: AccountAuthRequest):
    try:
        _launch_weixin_desktop()
        # Do not fail the capture if Weixin is installed elsewhere or already
        # running; the cache scan remains useful in either case.
        return account_downloader.capture(
            payload.source.strip(),
            alias=payload.alias,
            watch_seconds=min(max(payload.watch_seconds, 5), 60),
            include_temp=payload.include_temp,
        )
    except Exception as exc:
        error_text = str(exc)
        error_details = getattr(exc, "details", {}) or {}
        validation_error = str(error_details.get("last_validation_error", ""))
        if "ret=-3" in error_text or "ret=-3" in validation_error or "AUTH_REQUIRED" in error_text or "AUTH_REQUIRED" in validation_error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Weixin đã từ chối token đang lưu (ret=-3). Việc mở Weixin/đăng nhập chưa tạo request mới: "
                    "hãy dán link Official Account vào Weixin Desktop, mở trang tài khoản hoặc một bài viết, "
                    "rồi bấm Quét lại. Xem log tại logs/wechat_capture.log."
                ),
            ) from exc
        if "No fresh WeChat" in error_text or "Không tìm thấy phiên" in error_text:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Không thấy request Weixin mới. Hãy đăng nhập, mở Official Account/bài viết trong Weixin "
                    "rồi thử lại; nếu cần bật quét thư mục Temp. Xem log tại logs/wechat_capture.log."
                ),
            ) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/wechat/account/auth/log")
def account_auth_log(lines: int = 80):
    """Return the recent redacted capture log for diagnosing local WeChat scans."""
    lines = min(max(lines, 1), 300)
    if not CAPTURE_LOG_PATH.is_file():
        return {"path": str(CAPTURE_LOG_PATH), "lines": []}
    content = CAPTURE_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": str(CAPTURE_LOG_PATH), "lines": content[-lines:]}


def _safe_account_name(value: str) -> str:
    safe = "".join(char for char in value if char.isalnum() or char in (" ", "-", "_", "")).strip()
    return safe or "wechat-account"


def _process_account_download(download: dict[str, object]) -> dict[str, object]:
    """Move one upstream export into the app's report layout."""
    source_dir = Path(str(download["directory"])).resolve()
    article_meta = dict(download.get("article") or {})
    title = str(article_meta.get("title") or "Báo cáo WeChat")
    account_name = str(article_meta.get("account_name") or "wechat-account")
    published_at = str(article_meta.get("published_at") or "")
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00")) if published_at else datetime.now()
    except ValueError:
        published = datetime.now()
    now = datetime.now()
    raw_root = BASE_DIR / "storage" / "raw"
    saved_path = raw_root / _safe_account_name(account_name) / str(published.year) / f"{published.month:02d}" / f"{published.day:02d}" / f"{now.strftime('%H%M%S%f')}-{uuid4().hex}"
    saved_path.mkdir(parents=True, exist_ok=False)
    source_md = source_dir / "article.md"
    if not source_md.is_file():
        raise ValueError(f"Không tìm thấy Markdown do bộ tải tạo ra: {source_md}")
    content = source_md.read_text(encoding="utf-8")
    shutil.copy2(source_md, saved_path / "article.md")
    if (source_dir / "article.html").is_file():
        shutil.copy2(source_dir / "article.html", saved_path / "article.html")
    if (source_dir / "assets").is_dir():
        shutil.copytree(source_dir / "assets", saved_path / "assets", dirs_exist_ok=True)
        public_prefix = "/stored/" + "/".join(saved_path.relative_to(BASE_DIR).parts)
        content = re.sub(r"\]\((assets/[^)]+)\)", rf"]({public_prefix}/\1)", content)
        (saved_path / "article.md").write_text(content, encoding="utf-8")
    assets = article_meta.get("assets") or []
    image_urls = [str(asset.get("remote_url")) for asset in assets if isinstance(asset, dict) and asset.get("remote_url")]
    metadata = {
        "title": title,
        "account_name": account_name,
        "author": article_meta.get("author"),
        "url": article_meta.get("url"),
        "published_at": published_at,
        "crawled_at": now.isoformat(),
        "status": "downloaded",
        "image_urls": image_urls,
        "assets": assets,
        "source": "wechat-article-downloader-skill",
    }
    (saved_path / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    report_data = article_report(metadata, content)
    analysis = report_data["analysis"]
    (saved_path / "analysis.json").write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (saved_path / "report.md").write_text(
        f"# {title}\n\n## Kết luận từ bài viết\n\n{analysis['report']['conclusion']}\n",
        encoding="utf-8",
    )
    relative = saved_path.relative_to(BASE_DIR / "storage" / "raw")
    return {
        "id": "/".join(relative.parts),
        "metadata": metadata,
        "content": content,
        "saved_dir": str(saved_path.relative_to(BASE_DIR)),
        "article_url": "/api/preview/" + "/".join(relative.parts),
        "analysis": analysis,
    }


@app.post("/api/wechat/account/download")
def account_download(payload: AccountRequest):
    source = payload.source.strip()
    if not source:
        raise HTTPException(status_code=400, detail="Hãy nhập URL kết quả Sogou hoặc URL bài WeChat trực tiếp.")
    if payload.latest is not None and payload.latest < 1:
        raise HTTPException(status_code=400, detail="Số bài mới nhất phải lớn hơn 0.")
    if "weixin.sogou.com/weixin" in source.lower():
        return _download_from_sogou_search(payload)
    try:
        batch = account_downloader.download_account(
            source,
            latest=payload.latest if not payload.target_date and not payload.since and not payload.until else None,
            on_date=payload.target_date or None,
            since=payload.since or None,
            until=payload.until or None,
        )
        downloads = batch.get("downloads") or []
        if not downloads:
            raise HTTPException(status_code=404, detail="Không tìm thấy bài viết phù hợp với lựa chọn ngày.")
        processed = [_process_account_download(item) for item in downloads]
        result = processed[0]
        result["account_batch"] = {
            "selected": len(processed),
            "discovered": len(batch.get("discovered") or []),
            "failures": batch.get("failures") or [],
            "history_scan": batch.get("history_scan") or {},
        }
        return result
    except HTTPException:
        raise
    except Exception as exc:
        error_text = str(exc)
        if "No credential named" in error_text or ("credential" in error_text.lower() and "stored" in error_text.lower()):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Chưa có phiên WeChat hợp lệ. Hãy bấm 'Lấy link xác thực', mở link trong Weixin Desktop "
                    "đã đăng nhập, rồi bấm 'Quét phiên WeChat' và chờ hoàn tất."
                ),
            ) from exc
        if "ret=-3" in error_text or "AUTH_REQUIRED" in error_text:
            raise HTTPException(
                status_code=409,
                detail=(
                    "WeChat đã từ chối phiên cũ (ret=-3). Hãy mở đúng Official Account trong Weixin Desktop, "
                    "bấm 'Lấy link xác thực' rồi 'Quét phiên WeChat' để ghi nhận phiên mới trước khi tìm bài. "
                    "Trong luồng Weixin, hệ thống không tự chuyển sang Sogou vì kết quả có thể là bài cũ; "
                    "nếu muốn dùng Sogou, hãy dán URL kết quả Sogou vào ô nguồn rồi chạy riêng."
                ),
            ) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _download_from_sogou_search(payload: AccountRequest):
    """Explicit Sogou mode; never used as a silent fallback for Weixin auth."""
    try:
        from app.wechat_browser import wechat_browser

        result = wechat_browser.search_url(payload.source, payload.target_date or "")
        urls = result.get("urls") or []
        if not urls and result.get("state") == "login_required":
            raise HTTPException(status_code=409, detail=result.get("message") or "Sogou yêu cầu CAPTCHA.")
        if not urls:
            raise HTTPException(status_code=404, detail="Không tìm thấy bài viết trong trang kết quả Sogou.")
        limit = payload.latest or 1
        processed = [_process_article_url(url) for url in urls[:limit]]
        output = processed[0]
        output["account_batch"] = {
            "selected": len(processed),
            "discovered": len(urls),
            "failures": [],
            "source": "sogou-explicit",
            "history_scan": {"complete": True, "page_url": result.get("page_url", "")},
        }
        return output
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không đọc được kết quả Sogou: {exc}") from exc


def _process_article_url(url: str, progress: Progress | None = None):
    try:
        _progress(progress, "Đang kết nối WeChat và tải toàn văn bài viết.")
        # The vendored exporter keeps the complete body, local images and
        # tables instead of only retaining remote image URLs.
        exported = account_downloader.service.download_article(
            url,
            formats="html,md,json",
            output_dir=account_downloader.output_dir,
            include_assets=True,
        )
        _progress(progress, "Đã tải nội dung. Đang lưu bài và đọc số liệu.")
        result = _process_account_download(exported)
    except Exception as upstream_error:
        logger.warning("Primary article downloader failed", exc_info=True)
        _progress(progress, "Trình tải chính gặp lỗi. Đang thử cách tải dự phòng.")
        # Keep the older lightweight downloader as a fallback for a URL that
        # the upstream parser cannot recognize yet.
        saved_dir = download_article(url, base_storage_dir=str(BASE_DIR / "storage" / "raw"))
        if not saved_dir:
            raise HTTPException(status_code=502, detail=f"Không tải được bài viết: {upstream_error}") from upstream_error
        _progress(progress, "Đã tải nội dung. Đang đọc kết luận và số liệu trong bài.")
        saved_path = Path(saved_dir)
        metadata = json.loads((saved_path / "metadata.json").read_text(encoding="utf-8"))
        content = (saved_path / "article.md").read_text(encoding="utf-8")
        report_data = article_report(metadata, content)
        analysis = report_data["analysis"]
        (saved_path / "analysis.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
        (saved_path / "report.md").write_text(f"# {metadata.get('title', 'Báo cáo')}\n\n## Kết luận từ bài viết\n\n{analysis['report']['conclusion']}\n", encoding="utf-8")
        result = {
            "id": "/".join(saved_path.relative_to(BASE_DIR / "storage" / "raw").parts),
            "metadata": metadata,
            "content": content,
            "saved_dir": str(saved_path.relative_to(BASE_DIR)),
            "article_url": "/api/preview/" + "/".join(str(saved_path.relative_to(BASE_DIR / "storage" / "raw")).replace("\\", "/").split("/")),
            "analysis": analysis,
        }
    _progress(progress, "Đã lưu bài viết và cập nhật dashboard.")
    result["analysis"] = _translate_saved_article(BASE_DIR / result["saved_dir"], progress)["analysis"]
    return result


def _read_clipboard():
    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.update()
        return root.clipboard_get().strip()
    except (tkinter.TclError, RuntimeError):
        return ""
    finally:
        if root is not None:
            root.destroy()


@app.get("/api/clipboard/status")
def clipboard_status():
    value = _read_clipboard()
    url = value if value.startswith(("https://mp.weixin.qq.com/s", "http://mp.weixin.qq.com/s")) else ""
    return {"has_url": bool(url), "url": url}


@app.post("/api/articles/from-clipboard")
def fetch_article_from_clipboard():
    url = _read_clipboard()
    if not url:
        raise HTTPException(status_code=400, detail="Clipboard không chứa văn bản")
    if not url.startswith(("https://mp.weixin.qq.com/s", "http://mp.weixin.qq.com/s")):
        raise HTTPException(status_code=400, detail="Clipboard chưa chứa link bài viết WeChat")
    return _process_article_url(url)


@app.post("/api/articles")
def fetch_article(payload: ArticleRequest):
    url = str(payload.url)
    if payload.url.scheme != "https" or payload.url.host != "mp.weixin.qq.com" or not re.match(r"^/s(?:/|$)", payload.url.path or ""):
        raise HTTPException(status_code=400, detail="Hãy dùng link bài viết https://mp.weixin.qq.com/s/...")
    return _process_article_url(url)


@app.post("/api/articles/jobs")
def fetch_article_job(payload: ArticleRequest):
    url = str(payload.url)
    if payload.url.scheme != "https" or payload.url.host != "mp.weixin.qq.com" or not re.match(r"^/s(?:/|$)", payload.url.path or ""):
        raise HTTPException(status_code=400, detail="Hãy dùng link bài viết https://mp.weixin.qq.com/s/...")
    return start_job("download", lambda progress: _process_article_url(url, progress))
