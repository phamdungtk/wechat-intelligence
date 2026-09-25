"""WeChat desktop bridge and clipboard article collector on Ubuntu."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

CONTAINER = "wechat-intelligence-desktop"
DISPLAY = ":1"
BASE_DIR = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger("wechat.desktop")
WECHAT_LINK = re.compile(r"https://mp\.weixin\.qq\.com/s(?:/|\?)[^\s<>\"']+")


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
    remote = "/tmp/wechat-intelligence-screen.png"
    docker_exec("scrot", "-z", "-o", remote)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "cp", f"{CONTAINER}:{remote}", str(output)], check=True, timeout=30)
    return output


def read_screen() -> list[dict]:
    remote = "/tmp/wechat-intelligence-screen.png"
    docker_exec("scrot", "-z", "-o", remote)
    result = docker_exec("tesseract", remote, "stdout", "-l", "chi_sim+eng", "--psm", "11", "tsv")
    rows = []
    for line in result.stdout.splitlines()[1:]:
        cells = line.split("\t", 11)
        if len(cells) == 12 and cells[-1].strip():
            rows.append({"text": cells[-1], "x": int(cells[6]), "y": int(cells[7]),
                         "width": int(cells[8]), "height": int(cells[9])})
    return rows


def click(x: int, y: int) -> None:
    geometry = docker_exec("xdotool", "getdisplaygeometry").stdout.split()
    if len(geometry) != 2 or not (0 <= x < int(geometry[0]) and 0 <= y < int(geometry[1])):
        raise ValueError("Tọa độ nằm ngoài màn hình WeChat")
    docker_exec("xdotool", "mousemove", str(x), str(y), "click", "1")


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

    return {str(metadata.get("url") or "") for _, metadata, _ in _saved_articles()}


def import_clipboard_article() -> dict | None:
    url = clipboard_article_url()
    if not url:
        return None
    from app.main import _saved_articles

    saved = None
    for metadata_path, metadata, _ in _saved_articles():
        if str(metadata.get("url") or "") == url:
            saved = (metadata_path, metadata, article_path)
            break
    if saved:
        from app.main import _translate_saved_article

        metadata_path, metadata, article_path = saved
        translated = _translate_saved_article(metadata_path.parent)
        LOGGER.info("Article already saved; report status=%s", translated["analysis"]["status"])
        return {"id": "/".join(metadata_path.parent.relative_to(BASE_DIR / "storage" / "raw").parts),
                "title": metadata.get("title", ""), "existing": True,
                "report_status": translated["analysis"]["status"]}
    from app.main import _process_article_url

    result = _process_article_url(url)
    LOGGER.info("Imported desktop article id=%s", result.get("id", ""))
    return {"id": result.get("id", ""), "title": result.get("metadata", {}).get("title", "")}


def screen_text() -> str:
    return " ".join(row["text"] for row in read_screen())


def fetch_latest_article() -> dict | None:
    """Open the newest article in WeChat Official Accounts and import its copied link."""
    state = screen_text()
    if "Official" not in state or "Accounts" not in state:
        # Close the embedded article browser; this returns to the Official Accounts feed.
        click(944, 54)
        time.sleep(1)
        state = screen_text()
    if "Official" not in state or "Accounts" not in state:
        raise RuntimeError("Không thấy trang Official Accounts trong WeChat")
    if "榴莲" not in state and "莲" not in state:
        raise RuntimeError("Không thấy bài mới của tài khoản 榴莲产业网")

    # The Official Accounts feed shows the newest message as its top article card.
    click(580, 230)
    time.sleep(8)
    state = screen_text()
    if not any(char.isdigit() for char in state):
        raise RuntimeError("Bài WeChat chưa mở được")

    try:
        click(845, 54)  # article browser menu
        time.sleep(0.5)
        if "CopyLink" not in screen_text().replace(" ", ""):
            LOGGER.warning("Không đọc được nhãn CopyLink; thử vị trí menu đã xác nhận")
        click(646, 117)
        time.sleep(0.5)
        url = clipboard_article_url()
        if not url:
            raise RuntimeError("WeChat không sao chép được link bài viết")
        LOGGER.info("Found latest WeChat article link: %s", url)
        result = import_clipboard_article()
        LOGGER.info("Latest WeChat article result: %s", result or "already saved")
        return result
    finally:
        # Return the desktop to the feed so the next scheduled run starts predictably.
        click(944, 54)


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
                url = clipboard_article_url()
                if url and url != previous:
                    import_clipboard_article()
                    previous = url
        except Exception:
            LOGGER.exception("Failed to import an article from the WeChat desktop clipboard")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Linux desktop bridge")
    parser.add_argument("command", choices=["status", "clipboard", "import-clipboard", "fetch-latest", "watch",
                                             "screenshot", "ocr", "click", "key", "paste"])
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--refresh-interval", type=float, default=3600.0)
    parser.add_argument("--output", type=Path, default=Path("/tmp/wechat-desktop.png"))
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--key")
    parser.add_argument("--text")
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
