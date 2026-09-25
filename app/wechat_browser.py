import html
import queue
import re
import threading
import tempfile
from concurrent.futures import Future
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = BASE_DIR / "storage" / "browser" / "wechat-profile"


def extract_article_urls(page_html: str):
    candidates = re.findall(r'https?:\\?/\\?/mp\.weixin\.qq\.com\\?/s[^"\'<> ]+', page_html)
    urls = []
    for candidate in candidates:
        url = html.unescape(candidate.replace("\\/", "/").replace("\\u0026", "&"))
        if "${" not in url and "window." not in url and url not in urls:
            urls.append(url)
    return urls


class WechatBrowser:
    def __init__(self):
        self._commands = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._status = {"state": "stopped", "message": "Chưa mở phiên WeChat", "connected": False}

    def status(self):
        with self._lock:
            return dict(self._status)

    def _set_status(self, **values):
        with self._lock:
            self._status.update(values)

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="wechat-browser", daemon=True)
        self._thread.start()

    def _submit(self, action, timeout=45, **payload):
        # If the previous Playwright operation failed, its daemon thread may
        # still be waiting with a dead BrowserContext. Start a clean worker for
        # this request so a closed Chrome page cannot poison all later retries.
        with self._lock:
            if self._status.get("state") == "error":
                self._thread = None
                self._status = {"state": "stopped", "message": "Đang mở lại phiên trình duyệt", "connected": False}
        self._ensure_thread()
        future = Future()
        self._commands.put((action, payload, future))
        return future.result(timeout=timeout)

    def open_login(self, history_url="", wechat_id=""):
        return self._submit("open_login", history_url=history_url, wechat_id=wechat_id)

    def latest_articles(self, history_url="", wechat_id="", target_date=""):
        return self._submit("latest", timeout=60, history_url=history_url, wechat_id=wechat_id, target_date=target_date)

    def search_url(self, search_url, target_date=""):
        return self._submit("search_url", timeout=90, search_url=search_url, target_date=target_date)

    def _run(self):
        playwright = sync_playwright().start()
        context = None
        headed = None
        try:
            while True:
                action, payload, future = self._commands.get()
                try:
                    # Sau khi người dùng đã xác minh trong Chrome hiển thị, giữ nguyên
                    # context đó cho các lần kiểm tra để không bị nhận diện như browser mới.
                    # Keep the explicit Sogou search visible as well: Sogou may
                    # show a CAPTCHA/verification page and the user must be able
                    # to complete it in the same persistent Chrome profile.
                    wants_headed = action in {"open_login", "search_url"} or headed is True
                    if context is None or headed != wants_headed:
                        if context:
                            context.close()
                        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                        try:
                            context = playwright.chromium.launch_persistent_context(
                                str(PROFILE_DIR),
                                channel="chrome",
                                headless=not wants_headed,
                                viewport={"width": 1280, "height": 850},
                                args=["--disable-blink-features=AutomationControlled"],
                            )
                        except Exception:
                            # A stale Chrome lock or a previous crashed profile
                            # should not make the public-search fallback unusable.
                            recovery = Path(tempfile.mkdtemp(prefix="wechat-profile-recovery-", dir=str(PROFILE_DIR.parent)))
                            context = playwright.chromium.launch_persistent_context(
                                str(recovery),
                                channel="chrome",
                                headless=not wants_headed,
                                viewport={"width": 1280, "height": 850},
                                args=["--disable-blink-features=AutomationControlled"],
                            )
                        headed = wants_headed
                    page = context.pages[0] if context.pages else context.new_page()
                    if action == "open_login":
                        result = self._open_login(page, payload)
                    elif action == "latest":
                        result = self._latest(page, context, payload)
                    elif action == "search_url":
                        result = self._search_url(page, context, payload)
                    else:
                        raise ValueError(f"Unknown browser action: {action}")
                    future.set_result(result)
                except Exception as exc:
                    self._set_status(state="error", message=str(exc), connected=False)
                    # A headed Chrome window can be closed by the user or by
                    # Chrome itself (profile lock/crash). Do not keep the
                    # dead BrowserContext around: the next request must start
                    # a fresh context instead of repeatedly raising
                    # "Target page, context or browser has been closed".
                    try:
                        if context:
                            context.close()
                    except Exception:
                        pass
                    context = None
                    headed = None
                    future.set_exception(exc)
        finally:
            if context:
                context.close()
            playwright.stop()

    def _open_login(self, page, payload):
        wechat_id = payload.get("wechat_id", "").strip()
        target = f"https://weixin.sogou.com/weixin?type=2&s_from=input&query={quote(wechat_id)}"
        self._goto(page, target)
        page.wait_for_timeout(1500)
        connected = "/antispider/" not in page.url
        self._set_status(
            state="ready" if connected else "awaiting_verification",
            message=(
                "Nguồn tìm kiếm đã sẵn sàng; hệ thống đang kiểm tra ngày đã chọn."
                if connected
                else "Nguồn tìm kiếm yêu cầu CAPTCHA. Hãy hoàn tất xác minh trong Chrome rồi bấm Kiểm tra bài mới."
            ),
            connected=connected,
            page_url=page.url,
            wechat_id=wechat_id,
        )
        return self.status()

    def _latest(self, page, context, payload):
        wechat_id = payload.get("wechat_id", "").strip()
        target = f"https://weixin.sogou.com/weixin?type=2&s_from=input&query={quote(wechat_id)}"
        self._goto(page, target)
        page.wait_for_timeout(2500)
        connected = "/antispider/" not in page.url
        urls = self._page_article_urls(page) if connected else []
        article_titles = {}
        selected_title = ""

        if connected and not urls:
            requested_date = payload.get("target_date", "").strip()
            today = date.fromisoformat(requested_date) if requested_date else datetime.now(ZoneInfo("Asia/Shanghai")).date()
            date_markers = {
                today.strftime("%Y-%m-%d"),
                f"{today.year}-{today.month}-{today.day}",
                f"{today.year}年{today.month}月{today.day}日",
                today.strftime("%m-%d"),
                f"{today.month}-{today.day}",
                f"{today.month}月{today.day}日",
                "今天",
            }
            account = None
            results = page.locator(".news-list li, .news-box li")
            for index in range(results.count()):
                candidate = results.nth(index)
                text = candidate.inner_text()
                if wechat_id in text and any(marker in text for marker in date_markers):
                    account = candidate
                    break
            account_link = account.locator("h3 a, .txt-box a").first if account is not None else page.locator(".no-today-result")
            if account_link.count():
                selected_title = account_link.inner_text().strip()
                href = account_link.get_attribute("href") or ""
                if href:
                    self._goto(page, urljoin(page.url, href))
                    page.wait_for_timeout(2500)
                    urls = self._page_article_urls(page)

        if connected and selected_title and not urls:
            article_link = page.locator("a[href*='/link?url='], a[href*='mp.weixin.qq.com/s']").first
            if article_link.count():
                href = article_link.get_attribute("href") or ""
                if href:
                    self._goto(page, urljoin(page.url, href))
                    page.wait_for_timeout(1800)
                    if "mp.weixin.qq.com/s" in page.url:
                        urls.append(page.url)
                    for url in self._page_article_urls(page):
                        if url not in urls:
                            urls.append(url)

        for url in urls:
            article_titles[url] = selected_title

        state = "connected" if connected else "login_required"
        message = (
            f"Đã tìm thấy {len(urls)} bài viết."
            if urls
            else (
                f"Không thấy bài ngày {today.strftime('%d/%m/%Y')} của {wechat_id} trên nguồn tìm kiếm."
                if connected
                else "Nguồn tìm kiếm đang yêu cầu CAPTCHA. Hãy bấm Kết nối nguồn bài và hoàn tất xác minh trong Chrome."
            )
        )
        self._set_status(state=state, message=message, connected=connected, page_url=page.url, wechat_id=wechat_id)
        return {"urls": urls, "article_titles": article_titles, "page_url": page.url, **self.status()}

    def _search_url(self, page, context, payload):
        """Resolve article links from a user-supplied Sogou result page."""
        search_url = payload.get("search_url", "").strip()
        target_date = payload.get("target_date", "").strip()
        self._goto(page, search_url)
        page.wait_for_timeout(1800)
        if "/antispider/" in page.url:
            self._set_status(
                state="login_required",
                connected=False,
                page_url=page.url,
                message="Sogou yêu cầu CAPTCHA; hãy xác minh trong cửa sổ Chrome rồi thử lại.",
            )
            return {"urls": [], "article_titles": {}, "page_url": page.url, **self.status()}

        markers = set()
        if target_date:
            requested = date.fromisoformat(target_date)
            markers = {
                requested.strftime("%Y-%m-%d"),
                f"{requested.year}年{requested.month}月{requested.day}日",
                requested.strftime("%m-%d"),
                f"{requested.month}月{requested.day}日",
            }
        urls = []
        titles = {}
        anchors = page.locator("a[href*='/link?url='], a[href*='mp.weixin.qq.com/s']")

        def resolve_candidates(date_markers):
            for index in range(min(anchors.count(), 20)):
                anchor = anchors.nth(index)
                item = anchor.locator("xpath=ancestor::li[1]")
                item_text = item.inner_text() if item.count() else anchor.inner_text()
                if date_markers and not any(marker in item_text for marker in date_markers):
                    continue
                href = anchor.get_attribute("href") or ""
                if not href:
                    continue
                resolved = urljoin(page.url, href)
                article_page = context.new_page()
                try:
                    self._goto(article_page, resolved)
                    article_page.wait_for_timeout(900)
                    if "mp.weixin.qq.com/s" in article_page.url and article_page.url not in urls:
                        urls.append(article_page.url)
                        titles[article_page.url] = anchor.inner_text().strip()
                finally:
                    article_page.close()
                if len(urls) >= 20:
                    break

        resolve_candidates(markers)
        # The date is sometimes rendered outside the <li> containing the
        # encrypted link. If that happens, do not report a false empty result:
        # Sogou already orders this page newest-first, so retry the visible
        # result links without the card-text filter.
        if not urls and markers:
            resolve_candidates(set())
        self._set_status(
            state="connected",
            connected=True,
            page_url=page.url,
            message=f"Đã tìm thấy {len(urls)} bài từ URL Sogou.",
        )
        return {"urls": urls, "article_titles": titles, "page_url": page.url, **self.status()}

    @staticmethod
    def _page_article_urls(page):
        urls = []
        if "mp.weixin.qq.com/s" in page.url and "${" not in page.url:
            urls.append(page.url)
        for anchor in page.locator("a[href]").all():
            href = anchor.get_attribute("href") or ""
            if "mp.weixin.qq.com/s" in href and "${" not in href and href not in urls:
                urls.append(href)
        for url in extract_article_urls(page.content()):
            if url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def _goto(page, url):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            if "ERR_ABORTED" not in str(exc):
                raise

wechat_browser = WechatBrowser()
