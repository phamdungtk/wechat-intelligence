"""Build monthly and quarterly reports from saved WeChat articles and daily counts."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import re
from uuid import uuid4

from app.reporting import article_report
from app.vietnamese_report import content_hash, generate_vietnamese_report, load_cached_report


def article_date(metadata: dict, metadata_path: Path) -> str | None:
    """Prefer the publication date; fall back to the saved article directory."""
    published = str(metadata.get("published_at") or "")[:10]
    try:
        return date.fromisoformat(published).isoformat()
    except ValueError:
        pass
    try:
        return date.fromisoformat("-".join(metadata_path.parent.parts[-4:-1])).isoformat()
    except ValueError:
        return None


def period_key(day: str, group: str) -> str:
    value = date.fromisoformat(day)
    if group == "month":
        return f"{value.year:04d}-{value.month:02d}"
    if group == "quarter":
        return f"{value.year:04d}-Q{(value.month - 1) // 3 + 1}"
    raise ValueError("Nhóm thời gian không hợp lệ")


def valid_period(group: str, key: str) -> bool:
    if group == "month" and re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", key):
        return True
    return bool(group == "quarter" and re.fullmatch(r"20\d{2}-Q[1-4]", key))


def _unique_articles(saved_articles: list[tuple[Path, dict, Path]]) -> list[tuple[Path, dict, Path, str]]:
    """Repeated downloads of one WeChat URL count as one article."""
    unique = []
    seen = set()
    for metadata_path, metadata, article_path in saved_articles:
        day = article_date(metadata, metadata_path)
        if not day:
            continue
        identity = str(metadata.get("url") or "").split("?", 1)[0] or str(metadata_path)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append((metadata_path, metadata, article_path, day))
    return unique


def available_periods(saved_articles: list[tuple[Path, dict, Path]], daily: list[dict]) -> dict:
    dates = [item[3] for item in _unique_articles(saved_articles)]
    dates.extend(row["date"] for row in daily if _valid_day(row.get("date")))
    return {
        "month": sorted({period_key(day, "month") for day in dates}, reverse=True),
        "quarter": sorted({period_key(day, "quarter") for day in dates}, reverse=True),
    }


def _valid_day(value: object) -> bool:
    try:
        date.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def _vehicle_totals(rows: list[dict]) -> dict:
    vietnam = sum(row["vietnam"] for row in rows)
    thailand = sum(row["thailand"] for row in rows)
    count = len(rows)
    return {
        "vietnam": vietnam,
        "thailand": thailand,
        "total": vietnam + thailand,
        "days_with_data": count,
        "average_per_recorded_day": round((vietnam + thailand) / count, 1) if count else None,
        "highest_day": max(rows, key=lambda row: row["total"]) if rows else None,
    }


def build_period_overview(
    saved_articles: list[tuple[Path, dict, Path]], daily: list[dict], raw_root: Path,
    group: str, key: str,
) -> dict:
    if not valid_period(group, key):
        raise ValueError("Kỳ báo cáo không hợp lệ")
    articles = []
    for metadata_path, metadata, article_path, day in _unique_articles(saved_articles):
        if period_key(day, group) != key:
            continue
        content = article_path.read_text(encoding="utf-8")
        cached = load_cached_report(metadata_path.parent, content)
        analysis = article_report(metadata, content, cached)["analysis"]
        report = analysis["report"]
        articles.append({
            "id": "/".join(metadata_path.parent.relative_to(raw_root).parts),
            "date": day,
            "title": metadata.get("title", ""),
            "account_name": metadata.get("account_name", ""),
            "source_url": metadata.get("url", ""),
            "summary": report["summary"],
            "conclusion": report["conclusion"] if analysis["status"] == "completed" else "",
            "report_status": analysis["status"],
        })
    articles.sort(key=lambda item: (item["date"], item["title"]), reverse=True)
    rows = [row for row in daily if _valid_day(row.get("date")) and period_key(row["date"], group) == key]
    rows.sort(key=lambda row: row["date"], reverse=True)
    subgroups = defaultdict(lambda: {"articles": 0, "vehicle_rows": []})
    if group == "quarter":
        for article in articles:
            subgroups[article["date"][:7]]["articles"] += 1
        for row in rows:
            subgroups[row["date"][:7]]["vehicle_rows"].append(row)
    return {
        "group": group,
        "key": key,
        "article_count": len(articles),
        "accounts": dict(Counter(item["account_name"] for item in articles)),
        "articles": articles,
        "vehicles": _vehicle_totals(rows),
        "daily": rows,
        "months": [
            {"month": month, "article_count": data["articles"], "vehicles": _vehicle_totals(data["vehicle_rows"])}
            for month, data in sorted(subgroups.items())
        ],
    }


def period_report_input(overview: dict) -> str:
    """Keep every translated conclusion in the model input, within a fixed budget."""
    articles = [item for item in overview["articles"] if item["conclusion"]]
    if not articles:
        return ""
    per_article = max(180, min(1200, 48000 // len(articles)))
    blocks = []
    for item in reversed(articles):
        text = (item["conclusion"] + "\n" + item["summary"]).strip()[:per_article]
        blocks.append(f"Ngày {item['date']} | {item['title']} | {item['account_name']}\n{text}")
    return "\n\n".join(blocks)


def load_period_report(storage_root: Path, overview: dict) -> dict | None:
    source = period_report_input(overview)
    if not source:
        return None
    path = storage_root / "period_reports" / overview["group"] / f"{overview['key']}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("content_sha256") == content_hash(source) and data.get("summary") and data.get("conclusion"):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def save_period_report(storage_root: Path, overview: dict) -> dict:
    source = period_report_input(overview)
    if not source:
        raise ValueError("Chưa có kết luận tiếng Việt trong các bài của kỳ này")
    title = f"Tổng hợp {overview['key']} từ {overview['article_count']} bài WeChat"
    report = generate_vietnamese_report(title, source, period=True)
    report["source_article_count"] = sum(bool(item["conclusion"]) for item in overview["articles"])
    path = storage_root / "period_reports" / overview["group"] / f"{overview['key']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return report
