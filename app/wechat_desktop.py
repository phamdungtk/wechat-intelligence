"""WeChat desktop bridge and clipboard article collector on Ubuntu."""

from __future__ import annotations

import argparse
import calendar
from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

CONTAINER = "wechat-intelligence-desktop"
DISPLAY = ":1"
BASE_DIR = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger("wechat.desktop")
WECHAT_LINK = re.compile(r"https://mp\.weixin\.qq\.com/s(?:/|\?)[^\s<>\"']+")
TIMEZONE = ZoneInfo("Asia/Bangkok")


def docker_exec(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", "-e", f"DISPLAY={DISPLAY}", CONTAINER, *args],
        text=True, capture_output=True, check=check, timeout=30,
    )


def desktop_status() -> dict:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        text=True, capture_output=True, timeout=10,
    )
    return {"container": CONTAINER, "state": result.stdout.strip() if result.returncode == 0 else "not_installed"}


def desktop_login_state() -> str:
    rows = read_screen()
    text = " ".join(row["text"] for row in rows).lower()
    if "official" in text and "accounts" in text:
        return "logged_in"
    has_article_date = any(re.match(r"^20\d{2}[-/]\d{2}[-/]\d{2}$", row["text"]) for row in rows)
    if "榴莲" in text and (has_article_date or any(
        marker in text for marker in ("文章", "已关注", "私信")
    )):
        # The account profile/article archive is also an authenticated WeChat
        # screen, even though it no longer displays the English chat label.
        return "logged_in"
    if "confirm" in text and "phone" in text:
        return "confirm_phone"
    account_picker = (
        any(row["text"].lower() in {"wechat", "echat"} and 190 <= row["y"] <= 225
            and 380 <= row["x"] <= 455 for row in rows)
        and any(350 <= row["y"] <= 420 and 450 <= row["x"] <= 570
                and len(row["text"]) >= 3 for row in rows)
    )
    if "log in" in text or "login" in text or account_picker:
        return "login_required"
    if "qr" in text or "scan" in text or "扫描" in text or "二维码" in text:
        return "scan_qr"
    if len(rows) > 12:
        # An authenticated chat/article page may not contain the account name
        # or English navigation labels. Login/splash screens are handled above
        # and contain only a few OCR rows.
        return "logged_in"
    return "starting"


def prepare_desktop_login() -> str:
    with desktop_lock():
        state = desktop_login_state()
        if state == "starting":
            subprocess.run(["docker", "restart", CONTAINER], check=True, capture_output=True,
                           text=True, timeout=60)
            for _ in range(15):
                time.sleep(2)
                try:
                    state = desktop_login_state()
                except subprocess.SubprocessError:
                    continue
                if state != "starting":
                    break
        if state == "login_required":
            click(512, 475)
            time.sleep(2)
            state = desktop_login_state()
        return state


def clipboard_article_url() -> str | None:
    result = docker_exec("xclip", "-selection", "clipboard", "-o", check=False)
    if result.returncode:
        return None
    match = WECHAT_LINK.search(result.stdout.replace("&amp;", "&"))
    if not match:
        return None
    url = match.group(0).rstrip(".,;)")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com":
        return None
    return url


def screenshot(output: Path) -> Path:
    remote = _screen_remote_path()
    docker_exec("scrot", "-z", "-o", remote)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "cp", f"{CONTAINER}:{remote}", str(output)], check=True, timeout=30)
    return output


def read_screen() -> list[dict]:
    remote = _screen_remote_path()
    docker_exec("scrot", "-z", "-o", remote)
    result = docker_exec("tesseract", remote, "stdout", "-l", "chi_sim+eng", "--psm", "11", "tsv")
    rows = []
    for line in result.stdout.splitlines()[1:]:
        cells = line.split("\t", 11)
        if len(cells) == 12 and cells[-1].strip():
            rows.append({"text": cells[-1], "x": int(cells[6]), "y": int(cells[7]),
                         "width": int(cells[8]), "height": int(cells[9])})
    return rows


def _screen_remote_path() -> str:
    # Login polling and archive sync can capture the desktop concurrently.
    # Give each worker thread its own screenshot path to avoid truncated PNGs.
    return f"/tmp/wechat-intelligence-screen-{os.getpid()}-{threading.get_ident()}.png"


def click(x: int, y: int) -> None:
    geometry = docker_exec("xdotool", "getdisplaygeometry").stdout.split()
    if len(geometry) != 2 or not (0 <= x < int(geometry[0]) and 0 <= y < int(geometry[1])):
        raise ValueError("Tọa độ nằm ngoài màn hình WeChat")
    docker_exec("xdotool", "mousemove", str(x), str(y), "click", "1")


def _browser_windows() -> list[str]:
    result = docker_exec("xdotool", "search", "--onlyvisible", "--name", "^WeChat$", check=False)
    return [window_id for window_id in result.stdout.split() if window_id.isdigit()]


def _position_browser() -> bool:
    windows = _browser_windows()
    if not windows:
        return False
    # WeChat's main chat and its embedded article browser share the same title.
    # The article browser is an offset popup; prefer it over the main window,
    # which can occupy the full display.
    window_sizes = []
    for window_id in windows:
        geometry = docker_exec("xdotool", "getwindowgeometry", "--shell", window_id, check=False).stdout
        x = re.search(r"^X=(-?\d+)$", geometry, re.MULTILINE)
        y = re.search(r"^Y=(-?\d+)$", geometry, re.MULTILINE)
        width = re.search(r"^WIDTH=(\d+)$", geometry, re.MULTILINE)
        height = re.search(r"^HEIGHT=(\d+)$", geometry, re.MULTILINE)
        if x and y and width and height:
            area = int(width.group(1)) * int(height.group(1))
            # The embedded article browser is an offset popup. The main WeChat
            # shell can fill the whole display and must not win by area.
            popup = int(x.group(1)) > 0 or int(y.group(1)) > 0
            window_sizes.append((popup, area, window_id))
    window_id = max(window_sizes)[2] if window_sizes else windows[-1]
    docker_exec("xdotool", "windowmove", window_id, "51", "34", check=False)
    docker_exec("xdotool", "windowraise", window_id, check=False)
    docker_exec("xdotool", "windowactivate", "--sync", window_id, check=False)
    return True


def _close_article_tab() -> None:
    if _position_browser():
        # Focus the embedded browser before Ctrl+W; without activation the key
        # can go to the main chat window and leave the article tab in front.
        docker_exec("xdotool", "key", "--clearmodifiers", "ctrl+w", check=False)
        time.sleep(0.3)


def keypress(key: str) -> None:
    allowed = {"Return", "Escape", "Tab", "ctrl+c", "ctrl+v", "ctrl+a", "ctrl+f", "BackSpace"}
    if key not in allowed:
        raise ValueError("Phím không được phép")
    docker_exec("xdotool", "key", "--clearmodifiers", key)


def paste(text: str) -> None:
    subprocess.run(
        ["docker", "exec", "-i", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
         "xclip", "-selection", "clipboard"],
        input=text, text=True, capture_output=True, check=True, timeout=30,
    )
    keypress("ctrl+v")


def saved_article_urls() -> set[str]:
    from app.main import _saved_articles

    return {_canonical_url(str(metadata.get("url") or "")) for _, metadata, _ in _saved_articles()}


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.startswith("/s/"):
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return url.split("#", 1)[0]


def import_clipboard_article(progress=None) -> dict | None:
    url = clipboard_article_url()
    if not url:
        return None
    from app.main import _saved_articles

    saved = None
    for metadata_path, metadata, article_path in _saved_articles():
        if _canonical_url(str(metadata.get("url") or "")) == _canonical_url(url):
            saved = (metadata_path, metadata, article_path)
            break
    if saved:
        from app.main import _translate_saved_article

        metadata_path, metadata, _ = saved
        translated = _translate_saved_article(metadata_path.parent, progress)
        LOGGER.info("Article already saved; report status=%s", translated["analysis"]["status"])
        return {"id": "/".join(metadata_path.parent.relative_to(BASE_DIR / "storage" / "raw").parts),
                "title": metadata.get("title", ""), "existing": True,
                "report_status": translated["analysis"]["status"]}
    from app.main import _process_article_url

    result = _process_article_url(url, progress)
    LOGGER.info("Imported desktop article id=%s", result.get("id", ""))
    return {"id": result.get("id", ""), "title": result.get("metadata", {}).get("title", "")}


def screen_text() -> str:
    return " ".join(row["text"] for row in read_screen())


@contextmanager
def desktop_lock():
    import fcntl

    lock_path = BASE_DIR / "storage" / "wechat_desktop" / "automation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("WeChat đang được đồng bộ ở một tác vụ khác.") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fetch_latest_article() -> dict | None:
    """Open the newest article in WeChat Official Accounts and import its copied link."""
    state = screen_text()
    if "Official" not in state or "Accounts" not in state:
        raise RuntimeError(f"WeChat chưa ở trang Official Accounts (màn hình hiện tại: {state[:240]})")
    if "榴莲" not in state and "莲" not in state:
        raise RuntimeError("Không thấy bài mới của tài khoản 榴莲产业网")

    click(580, 230)
    time.sleep(8)
    state = screen_text()
    if not any(char.isdigit() for char in state):
        raise RuntimeError("Bài WeChat chưa mở được")
    try:
        url = _copy_open_article_url()
        LOGGER.info("Found latest WeChat article link: %s", url)
        result = import_clipboard_article()
        LOGGER.info("Latest WeChat article result: %s", result or "already saved")
        return result
    finally:
        _close_article_tab()


def fetch_latest_article() -> dict | None:
    with desktop_lock():
        return _fetch_latest_article()


def _open_account_archive(progress=None) -> None:
    if progress:
        progress("Đang mở kho bài của tài khoản 榴莲产业网.")

    rows = read_screen()
    text = " ".join(row["text"] for row in rows)
    has_article_date = any(re.match(r"^20\d{2}[-/]\d{2}[-/]\d{2}$", row["text"]) for row in rows)
    profile_open = "榴莲" in text and (has_article_date or any(
        marker in text for marker in ("文章", "已关注", "关注", "私信", "View History")
    ))
    if not profile_open:
        # Open Contacts, then select the account from the followed Official
        # Accounts list. The “Official Accounts” chat is only a Top Stories
        # feed and can contain unrelated publishers.
        click(31, 164)
        time.sleep(0.5)
        rows = read_screen()

        def account_row(items):
            return next((row for row in items
                         if ("榴莲" in row["text"] or "产业网" in row["text"])
                         and 70 <= row["x"] <= 300 and 150 <= row["y"] <= 700), None)

        def followed_account_y(items):
            category = next((row for row in items
                             if ("official" in row["text"].lower()
                                 or "accounts" in row["text"].lower())
                             and 70 <= row["x"] <= 300 and 70 <= row["y"] <= 205), None)
            if category is None:
                return None
            # The followed list contains one account, sorted below the “L”
            # heading (榴莲, pinyin Liu). Tesseract often misreads its small
            # Chinese label, so use the list structure as a fallback.
            letter = next((row for row in items
                           if row["text"].strip().lower() == "l"
                           and 85 <= row["x"] <= 125
                           and category["y"] < row["y"] <= category["y"] + 55), None)
            return letter["y"] + 37 if letter else None

        target = account_row(rows)
        target_y = followed_account_y(rows)
        if target is None and target_y is None:
            category_rows = [row for row in rows
                             if ("official" in row["text"].lower()
                                 or "accounts" in row["text"].lower())
                             and 70 <= row["x"] <= 300 and 70 <= row["y"] <= 205]
            if category_rows:
                category_y = round(sum(row["y"] + row["height"] // 2
                                       for row in category_rows) / len(category_rows))
                click(165, category_y)
                time.sleep(0.5)
                rows = read_screen()
                target = account_row(rows)
                target_y = followed_account_y(rows)

        if target is None and target_y is None:
            state = " ".join(row["text"] for row in rows)
            raise RuntimeError(
                f"Không tìm thấy 榴莲产业网 trong danh sách Official Accounts đã theo dõi "
                f"(màn hình: {state[:240]})"
            )

        if target:
            click(max(140, min(230, target["x"] + target["width"] // 2)),
                  target["y"] + max(5, target["height"] // 2))
        else:
            click(170, target_y)
        time.sleep(1)
        rows = read_screen()
        profile_text = " ".join(row["text"] for row in rows)
        if not any(marker in profile_text.lower()
                   for marker in ("榴莲", "产业网", "durianindustry")):
            raise RuntimeError(
                f"Đã chọn tài khoản nhưng chưa mở được hồ sơ 榴莲产业网 "
                f"(màn hình: {profile_text[:240]})"
            )
    article_tab = next((row for row in rows if "文章" in row["text"] and 300 <= row["x"] <= 600
                        and 60 <= row["y"] <= 500), None)
    if article_tab is None and "榴莲" in " ".join(row["text"] for row in rows):
        # A previously opened native Friend Profile needs one extra step before
        # the embedded history page exposes its tabs.
        history_button = next((row for row in rows
                               if "history" in row["text"].lower()
                               or "历史" in row["text"]), None)
        if history_button:
            click(history_button["x"] + history_button["width"] // 2,
                  history_button["y"] + history_button["height"] // 2)
        else:
            # The profile is verified by its account name; this is the fixed
            # View History button in the 1024x768 native profile layout.
            click(664, 390)
        time.sleep(2)
        rows = read_screen()

    article_tab = next((row for row in rows if "文章" in row["text"] and 300 <= row["x"] <= 600
                        and 60 <= row["y"] <= 500), None)
    if article_tab is None and not profile_open:
        state = " ".join(row["text"] for row in rows)
        raise RuntimeError(f"Đã mở WeChat nhưng không thấy thẻ bài viết của Official Account (màn hình: {state[:240]})")
    if article_tab:
        click(article_tab["x"] + max(5, article_tab["width"] // 2), article_tab["y"] + 7)
        time.sleep(1)
    _position_browser()


def _archive_card_groups(image) -> list[dict]:
    """Locate complete article bundles in the fixed WeChat Linux archive view."""
    if image.size != (1024, 768):
        raise RuntimeError("Màn hình WeChat đã đổi độ phân giải; cần kiểm tra lại bố cục kho bài.")
    # The archive uses a vertical grey gradient, so compare each row against
    # the background at that same height, outside the article cards.
    occupied = [any(max(abs(channel - background) for channel, background in
                        zip(image.getpixel((x, y))[:3], image.getpixel((720, y))[:3])) >= 7
                    for x in (320, 700)) for y in range(150, 703)]
    # Image gradients can briefly match the grey page background within a card.
    for i in range(1, len(occupied) - 8):
        if occupied[i - 1] and not occupied[i]:
            end = i
            while end < len(occupied) and not occupied[end] and end - i <= 8:
                end += 1
            if end < len(occupied) and occupied[end] and end - i <= 8:
                occupied[i:end] = [True] * (end - i)
    groups = []
    start = None
    for index, active in enumerate(occupied + [False]):
        y = 150 + index
        if active and start is None:
            start = y
        if not active and start is not None:
            end = y - 1
            height = end - start + 1
            extras = round((height - 202) / 96)
            short_card = 80 <= height <= 130
            standard_card = (0 <= extras <= 8
                             and abs(height - (202 + extras * 96)) <= 24)
            if start > 155 and end < 697 and (short_card or standard_card):
                crop = image.crop((320, start, 705, end + 1))
                # Opening a card increments its read counter. Exclude those
                # counters so the same card keeps one identity while scrolling.
                crop.paste((255, 255, 255), (0, 170, 170, min(height, 201)))
                for extra_index in range(extras):
                    top = 263 + 96 * extra_index
                    crop.paste((255, 255, 255), (0, top, 170, min(height, top + 30)))
                signature = hashlib.sha256(crop.tobytes()).hexdigest()
                positions = ([start + height // 2] if short_card else
                             [start + 115] + [start + 202 + 48 + 96 * i for i in range(extras)])
                groups.append({"signature": signature, "positions": positions})
            start = None
    return groups


def _set_clipboard_marker() -> None:
    subprocess.run(
        ["docker", "exec", "-i", "-e", f"DISPLAY={DISPLAY}", CONTAINER,
         "xclip", "-selection", "clipboard"],
        input="WECHAT_SYNC_WAITING_FOR_COPY_LINK", text=True,
        capture_output=True, check=True, timeout=15,
    )


def _copy_open_article_url() -> str:
    """Wait for the article menu's Copy Link item, then read a fresh URL."""
    _set_clipboard_marker()
    for attempt in range(10):
        time.sleep(1.5 if attempt else 2.5)
        if not _position_browser():
            raise RuntimeError("Không thấy cửa sổ bài viết WeChat đang mở.")
        click(845, 54)
        time.sleep(0.3)
        rows = read_screen()
        copy_row = next((row for row in rows if row["text"].lower().replace(" ", "") in {"copy", "copylink"}
                         and 100 <= row["y"] <= 145 and 510 <= row["x"] <= 700), None)
        link_row = next((row for row in rows if row["text"].lower() == "link"
                         and copy_row and abs(row["y"] - copy_row["y"]) <= 6), None)
        if copy_row and (link_row or copy_row["text"].lower().replace(" ", "") == "copylink"):
            click(copy_row["x"] + 15, copy_row["y"] + 8)
            time.sleep(0.3)
            url = clipboard_article_url()
            if url:
                return url
            raise RuntimeError("WeChat hiện Copy Link nhưng không sao chép được URL bài viết.")
        click(845, 54)  # Dismiss the menu while the article is still loading.
    raise RuntimeError("Bài WeChat chưa tải xong hoặc không có mục Copy Link.")


def _period_bounds(group: str, key: str) -> tuple[date, date]:
    from app.period_overview import valid_period

    if not valid_period(group, key):
        raise ValueError("Kỳ đồng bộ không hợp lệ")
    year = int(key[:4])
    if group == "month":
        month = int(key[-2:])
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    quarter = int(key[-1])
    first_month = (quarter - 1) * 3 + 1
    last_month = first_month + 2
    return date(year, first_month, 1), date(year, last_month, calendar.monthrange(year, last_month)[1])


def sync_period_articles(group: str, key: str, progress=None, *, max_scrolls: int = 1500,
                         exact_date: date | None = None) -> dict:
    """Walk the account's archive until it passes a chosen month or quarter."""
    from PIL import Image
    from app.main import BASE_DIR as APP_BASE_DIR, _process_article_url, _saved_articles
    from app.period_overview import article_date
    from app.wechat_account import WeChatClient

    start, end = _period_bounds(group, key)
    if exact_date is not None:
        start = end = exact_date
    if desktop_status()["state"] != "running":
        raise RuntimeError("WeChat Desktop trên Ubuntu chưa chạy.")
    result = {"group": group, "key": key, "scanned": 0, "matched": 0, "imported": 0,
              "skipped_existing": 0, "skipped_outside": 0, "failures": [],
              "items": [], "complete": False}
    known = {_canonical_url(str(metadata.get("url") or "")): (path, metadata)
             for path, metadata, _ in _saved_articles()}
    seen_urls: set[str] = set()
    seen_groups: set[str] = set()
    client = WeChatClient(timeout=25, interval_seconds=0.5)
    with desktop_lock():
        _open_account_archive(progress)
        stagnant = 0
        previous_screen = None
        older_groups = 0
        try:
            for step in range(max_scrolls):
                image = Image.open(screenshot(Path("/tmp/wechat-archive-scan.png"))).convert("RGB")
                screen_hash = hashlib.sha256(image.crop((315, 150, 709, 703)).tobytes()).hexdigest()
                stagnant = stagnant + 1 if screen_hash == previous_screen else 0
                previous_screen = screen_hash
                for card in _archive_card_groups(image):
                    if card["signature"] in seen_groups:
                        continue
                    seen_groups.add(card["signature"])
                    card_dates = []
                    for position in card["positions"]:
                        try:
                            click(500, position)
                            url = _copy_open_article_url()
                        except Exception as exc:
                            message = f"Không đọc được một thẻ bài ở vị trí {position}: {exc}"
                            result["failures"].append(message)
                            if progress:
                                progress(message)
                            continue
                        finally:
                            # Each archive card opens a second browser tab. Closing it
                            # restores the archive and its scroll position.
                            _close_article_tab()
                        identity = _canonical_url(url)
                        if identity in seen_urls:
                            continue
                        seen_urls.add(identity)
                        result["scanned"] += 1
                        saved = known.get(identity)
                        published = article_date(saved[1], saved[0]) if saved else None
                        article = None
                        if not published:
                            try:
                                article = client.fetch_article(url)
                                published = article.published_at.date().isoformat() if article.published_at else None
                            except Exception as exc:
                                message = f"Không đọc được ngày phát hành của bài {url}: {exc}"
                                result["failures"].append(message)
                                if progress:
                                    progress(message)
                                continue
                        if not published:
                            message = f"Bài {url} không có ngày phát hành, đã bỏ qua."
                            result["failures"].append(message)
                            if progress:
                                progress(message)
                            continue
                        article_day = date.fromisoformat(published)
                        card_dates.append(article_day)
                        if not start <= article_day <= end:
                            result["skipped_outside"] += 1
                            continue
                        result["matched"] += 1
                        if saved:
                            result["skipped_existing"] += 1
                            if progress:
                                progress(f"{published}: đã có bài, bỏ qua {saved[1].get('title', '')}.")
                            continue
                        title = article.title if article else url
                        if progress:
                            progress(f"{published}: đang tải {title}.")
                        try:
                            imported = _process_article_url(url, progress)
                            metadata_path = APP_BASE_DIR / imported["saved_dir"] / "metadata.json"
                            imported_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                            if not imported_metadata.get("published_at"):
                                imported_metadata["published_at"] = article.published_at.isoformat()
                                metadata_path.write_text(
                                    json.dumps(imported_metadata, ensure_ascii=False, indent=2), encoding="utf-8",
                                )
                            result["imported"] += 1
                            result["items"].append({"date": published, "id": imported.get("id"), "title": title})
                            known[identity] = (metadata_path, imported_metadata)
                        except Exception as exc:
                            message = f"Không tải được bài {published} {title}: {exc}"
                            result["failures"].append(message)
                            if progress:
                                progress(message)
                    if card_dates and max(card_dates) < start and len(seen_groups) > 1:
                        older_groups += 1
                    else:
                        older_groups = 0
                    if older_groups >= 2:
                        result["complete"] = not result["failures"]
                        if progress:
                            progress("Đã đi qua đầu kỳ cần lấy; hoàn tất quét kho bài.")
                        return result
                if stagnant >= 3:
                    result["complete"] = not result["failures"]
                    if progress:
                        progress("Đã tới cuối kho bài WeChat.")
                    return result
                if step and step % 20 == 0 and progress:
                    progress(f"Đang quét kho bài: đã xem {result['scanned']} link, lưu thêm {result['imported']} bài.")
                docker_exec("xdotool", "mousemove", "850", "590", "key", "Down")
                time.sleep(0.15)
            result["failures"].append("Đã đạt giới hạn cuộn kho bài trước khi hết kỳ.")
            return result
        finally:
            # Keep the authenticated WeChat window open on the archive screen.
            pass


def sync_today_articles(progress=None) -> dict:
    today = datetime.now(TIMEZONE).date()
    result = sync_period_articles("month", today.isoformat()[:7], progress, exact_date=today)
    return {**result, "date": today.isoformat(), "synced": result["matched"]}


def watch_clipboard(interval: float = 5.0, refresh_interval: float = 3600.0) -> None:
    previous = None
    next_refresh = 0.0
    while True:
        try:
            if desktop_status()["state"] == "running":
                now = time.monotonic()
                if now >= next_refresh:
                    fetch_latest_article()
                    next_refresh = now + max(60.0, refresh_interval)
                try:
                    with desktop_lock():
                        url = clipboard_article_url()
                        if url and url != previous:
                            import_clipboard_article()
                            previous = url
                except RuntimeError:
                    # The web button owns the WeChat window and clipboard.
                    pass
        except Exception:
            LOGGER.exception("Failed to import an article from the WeChat desktop clipboard")
            next_refresh = time.monotonic() + max(300.0, refresh_interval)
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Linux desktop bridge")
    parser.add_argument("command", choices=["status", "clipboard", "import-clipboard", "fetch-latest", "sync-today", "sync-period", "watch",
                                             "screenshot", "ocr", "click", "key", "paste"])
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--refresh-interval", type=float, default=3600.0)
    parser.add_argument("--output", type=Path, default=Path("/tmp/wechat-desktop.png"))
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--key")
    parser.add_argument("--text")
    parser.add_argument("--group", choices=["month", "quarter"])
    parser.add_argument("--period")
    parser.add_argument("--max-scrolls", type=int, default=1500)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "status":
        print(json.dumps(desktop_status(), ensure_ascii=False))
    elif args.command == "clipboard":
        print(clipboard_article_url() or "")
    elif args.command == "import-clipboard":
        print(json.dumps(import_clipboard_article(), ensure_ascii=False))
    elif args.command == "fetch-latest":
        print(json.dumps(fetch_latest_article(), ensure_ascii=False))
    elif args.command == "sync-today":
        print(json.dumps(sync_today_articles(), ensure_ascii=False))
    elif args.command == "sync-period":
        if not args.group or not args.period:
            parser.error("sync-period yêu cầu --group và --period")
        print(json.dumps(sync_period_articles(
            args.group, args.period,
            progress=lambda message: print(message, flush=True),
            max_scrolls=args.max_scrolls,
        ), ensure_ascii=False))
    elif args.command == "watch":
        watch_clipboard(max(1.0, args.interval), max(60.0, args.refresh_interval))
    elif args.command == "screenshot":
        print(screenshot(args.output))
    elif args.command == "ocr":
        print(json.dumps(read_screen(), ensure_ascii=False))
    elif args.command == "click":
        if args.x is None or args.y is None:
            parser.error("click yêu cầu --x và --y")
        click(args.x, args.y)
    elif args.command == "key":
        if not args.key:
            parser.error("key yêu cầu --key")
        keypress(args.key)
    elif args.command == "paste":
        if args.text is None:
            parser.error("paste yêu cầu --text")
        paste(args.text)


if __name__ == "__main__":
    main()
