"""Read Official Account history using a captured Weixin profile_ext session.

The upstream wechat-history project obtains these values from the authenticated
Weixin desktop request. This module keeps that protocol isolated and returns
normalised article links for the existing downloader pipeline.
"""

from datetime import datetime
from html import unescape
import json
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx


HISTORY_PATH = "https://mp.weixin.qq.com/mp/profile_ext"
TIMEZONE = ZoneInfo("Asia/Bangkok")
REQUIRED_KEYS = ("__biz", "uin", "key", "pass_ticket")


def credentials_from_source(source: str | dict[str, Any]) -> dict[str, str]:
    """Extract the four session parameters from a captured URL or JSON object."""
    if isinstance(source, dict):
        values = {str(key): str(value or "").strip() for key, value in source.items()}
    else:
        raw = str(source or "").strip()
        if not raw:
            raise ValueError("Chưa nhập URL hoặc JSON phiên Weixin")
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("JSON phiên Weixin không hợp lệ") from exc
            if not isinstance(parsed, dict):
                raise ValueError("JSON phiên Weixin phải là một object")
            values = {str(key): str(value or "").strip() for key, value in parsed.items()}
        else:
            query = parse_qs(urlparse(raw).query, keep_blank_values=True)
            values = {key: (items[0] if items else "").strip() for key, items in query.items()}

    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ValueError("Thiếu tham số phiên Weixin: " + ", ".join(missing))
    return {key: unquote(values[key]) for key in REQUIRED_KEYS}


def build_home_url(credentials: dict[str, str]) -> str:
    params = {
        "action": "home",
        "__biz": credentials["__biz"],
        "uin": credentials["uin"],
        "key": credentials["key"],
        "pass_ticket": credentials["pass_ticket"],
        "scene": "124",
        "devicetype": "Windows 10",
        "version": "6204014f",
        "lang": "en",
        "a8scene": "7",
        "winzoom": "1",
    }
    return HISTORY_PATH + "?" + "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _message_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    nested = _decode_json(payload.get("general_msg_list", {}))
    if not isinstance(nested, dict):
        return []
    messages = nested.get("list", [])
    return messages if isinstance(messages, list) else []


def _article_from_entry(entry: dict[str, Any], timestamp: int | float | None) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    info = entry.get("app_msg_ext_info") or entry
    if not isinstance(info, dict):
        return None
    url = unescape(str(info.get("content_url") or "")).strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return None
    try:
        published_at = datetime.fromtimestamp(float(timestamp or 0), tz=TIMEZONE)
    except (TypeError, ValueError, OSError):
        published_at = None
    return {
        "title": unescape(str(info.get("title") or "")).strip(),
        "url": url,
        "published_at": published_at.isoformat() if published_at else "",
        "published_date": published_at.date().isoformat() if published_at else "",
    }


def _normalise_posts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for message in _message_list(payload):
        if not isinstance(message, dict):
            continue
        info = message.get("comm_msg_info") or {}
        timestamp = info.get("datetime") if isinstance(info, dict) else None
        main = _article_from_entry(message, timestamp)
        if main:
            posts.append(main)
        ext = message.get("app_msg_ext_info") or {}
        children = ext.get("multi_app_msg_item_list", []) if isinstance(ext, dict) else []
        for child in children or []:
            article = _article_from_entry(child, timestamp)
            if article:
                posts.append(article)
    return posts


def fetch_posts(source: str | dict[str, Any], max_pages: int = 20) -> list[dict[str, Any]]:
    """Fetch recent posts through the authenticated profile_ext/getmsg flow."""
    credentials = credentials_from_source(source)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": "https://mp.weixin.qq.com/",
    }
    posts: list[dict[str, Any]] = []
    with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
        home = client.get(build_home_url(credentials))
        home.raise_for_status()
        offset = 0
        for page in range(max_pages):
            params = {"action": "getmsg", "__biz": credentials["__biz"], "offset": str(offset), "f": "json"}
            response = client.get(HISTORY_PATH, params=params)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Weixin không trả về JSON lịch sử; phiên có thể đã hết hạn") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("Dữ liệu lịch sử Weixin không hợp lệ")
            error = str(payload.get("errmsg") or "")
            if error and error.lower() != "ok":
                raise RuntimeError(f"Weixin trả về lỗi: {error}")
            page_posts = _normalise_posts(payload)
            posts.extend(page_posts)
            next_offset = payload.get("next_offset")
            if not payload.get("can_msg_continue") or not page_posts or next_offset in (None, offset):
                break
            offset = int(next_offset)
    unique: dict[str, dict[str, Any]] = {}
    for post in posts:
        unique.setdefault(post["url"], post)
    return sorted(unique.values(), key=lambda post: post.get("published_at", ""), reverse=True)


def latest_post(source: str | dict[str, Any], target_date: str | None = None) -> dict[str, Any]:
    posts = fetch_posts(source)
    wanted_date = target_date or datetime.now(TIMEZONE).date().isoformat()
    for post in posts:
        if post.get("published_date") == wanted_date:
            return post
    raise LookupError(f"Không tìm thấy bài Weixin nào đúng ngày {wanted_date}")
