"""Read-only WeChat desktop bridge and clipboard article collector on Ubuntu."""

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


def saved_article_urls() -> set[str]:
    from app.main import _saved_articles

    return {str(metadata.get("url") or "") for _, metadata, _ in _saved_articles()}


def import_clipboard_article() -> dict | None:
    url = clipboard_article_url()
    if not url or url in saved_article_urls():
        return None
    from app.main import _process_article_url

    result = _process_article_url(url)
    LOGGER.info("Imported desktop article id=%s", result.get("id", ""))
    return {"id": result.get("id", ""), "title": result.get("metadata", {}).get("title", "")}


def watch_clipboard(interval: float = 5.0) -> None:
    previous = None
    while True:
        try:
            if desktop_status()["state"] == "running":
                url = clipboard_article_url()
                if url and url != previous:
                    previous = url
                    import_clipboard_article()
        except Exception:
            LOGGER.exception("Failed to import an article from the WeChat desktop clipboard")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Linux desktop bridge")
    parser.add_argument("command", choices=["status", "clipboard", "import-clipboard", "watch"])
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "status":
        print(json.dumps(desktop_status(), ensure_ascii=False))
    elif args.command == "clipboard":
        print(clipboard_article_url() or "")
    elif args.command == "import-clipboard":
        print(json.dumps(import_clipboard_article(), ensure_ascii=False))
    else:
        watch_clipboard(max(1.0, args.interval))


if __name__ == "__main__":
    main()
