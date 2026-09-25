import json
import os
import re
from pathlib import Path
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from app.downloader.downloader import download_article
from app.reporting import article_report
from app.wechat_browser import wechat_browser


BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "storage" / "monitor_state.json"
CONFIG_FILE = BASE_DIR / "storage" / "monitor_config.json"
CAPTCHA_FILE = BASE_DIR / "storage" / "captcha.json"


def get_history_url():
    if os.getenv("WECHAT_HISTORY_URL", "").strip():
        return os.getenv("WECHAT_HISTORY_URL", "").strip()
    if CONFIG_FILE.exists():
        configured = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("history_url", "").strip()
        if configured:
            return configured
    # Fallback: lấy bizuin từ bài viết đã lưu để tự dựng trang lịch sử.
    articles = sorted((BASE_DIR / "storage" / "raw").glob("*/*/*/*/*/article.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    for article in articles:
        match = re.search(r"(?:var\s+bizuin|bizuin)\s*=\s*[\"']([^\"']+)", article.read_text(encoding="utf-8", errors="ignore"))
        if match:
            return f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={match.group(1)}"
    return ""


def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_urls": [], "last_run": None, "last_status": "not_started"}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def _save_config(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def get_account_query():
    config = _load_config()
    if config.get("account_name", "").strip():
        return config["account_name"].strip()
    metadata_files = sorted((BASE_DIR / "storage" / "raw").glob("*/*/*/*/*/metadata.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for metadata_file in metadata_files:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        account_name = metadata.get("account_name", "").strip()
        if account_name and account_name != "Unknown_Account":
            return account_name
    return config.get("wechat_id", "").strip()


def check_latest_articles(target_date=""):
    history_url = get_history_url()
    config = _load_config()
    wechat_id = get_account_query()
    state = _load_state()
    state["last_run"] = datetime.now().isoformat()

    if not history_url and not wechat_id:
        state["last_status"] = "missing_wechat_account"
        _save_state(state)
        logger.warning("Chưa cấu hình WeChat ID hoặc history URL")
        return {"saved": 0, "status": state["last_status"]}

    try:
        if config.get("browser_enabled"):
            browser_result = wechat_browser.latest_articles(history_url, wechat_id, target_date)
            found = browser_result.get("urls", [])
            article_titles = browser_result.get("article_titles", {})
            page_url = browser_result.get("page_url", "")
            if "mp.weixin.qq.com/mp/profile_ext" in page_url and page_url != history_url:
                config["history_url"] = page_url
                _save_config(config)
        else:
            response = httpx.get(history_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            found = []
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"]
                if "mp.weixin.qq.com/s/" in href:
                    url = href if href.startswith("http") else f"https://mp.weixin.qq.com{href}"
                    if url not in found:
                        found.append(url)

        saved = 0
        seen = set(state.get("seen_urls", []))
        seen_titles = set(state.get("seen_titles", []))
        # WeChat thường trả bài mới nhất trước; chỉ xử lý bài đầu tiên vì
        # hệ thống được cấu hình giữ lại báo cáo mới nhất.
        for url in found[:1]:
            discovered_title = article_titles.get(url, "") if "article_titles" in locals() else ""
            if url not in seen and discovered_title not in seen_titles and download_article(url, str(BASE_DIR / "storage" / "raw")):
                seen.add(url)
                if discovered_title:
                    seen_titles.add(discovered_title)
                saved += 1
                article_dirs = sorted((BASE_DIR / "storage" / "raw").glob("*/*/*/*/*"), key=lambda p: p.stat().st_mtime, reverse=True)
                if article_dirs:
                    article_dir = article_dirs[0]
                    content_path = article_dir / "article.md"
                    metadata_path = article_dir / "metadata.json"
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        report_data = article_report(metadata, content_path.read_text(encoding="utf-8"))
                        article_dir.joinpath("analysis.json").write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
                        conclusion = report_data["analysis"]["report"]["conclusion"]
                        article_dir.joinpath("report.md").write_text(f"# {metadata.get('title', 'Báo cáo')}\n\n## Kết luận từ bài viết\n\n{conclusion}\n", encoding="utf-8")
                    except Exception as exc:
                        logger.error(f"Lỗi tạo báo cáo bài mới: {exc}")
        state["seen_urls"] = list(seen)[-500:]
        state["seen_titles"] = list(seen_titles)[-500:]
        state["last_status"] = f"ok:{saved} saved, {len(found)} found"
    except Exception as exc:
        state["last_status"] = f"error:{exc}"
        logger.error(f"Lỗi kiểm tra bài WeChat: {exc}")

    _save_state(state)
    return {"saved": saved if "saved" in locals() else 0, "status": state["last_status"]}
