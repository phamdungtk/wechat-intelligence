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
                    import_clipboard_article()
                    previous = url
        except Exception:
            LOGGER.exception("Failed to import an article from the WeChat desktop clipboard")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat Linux desktop bridge")
    parser.add_argument("command", choices=["status", "clipboard", "import-clipboard", "watch",
                                             "screenshot", "ocr", "click", "key", "paste"])
    parser.add_argument("--interval", type=float, default=5.0)
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
    elif args.command == "watch":
        watch_clipboard(max(1.0, args.interval))
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
