"""Read conclusions and daily land-port counts directly from saved articles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from app.vietnamese_report import contains_chinese


def source_conclusion(content: str) -> str:
    """Return the article's own Vietnamese conclusion or DIN view, if present."""
    lines = content.splitlines()
    headings = (
        re.compile(r"^#{1,6}\s+(?:\*\*)?(?:\d+[.)]\s*)?kết luận\b", re.I),
        re.compile(r"^#{1,6}\s+.*(?:góc nhìn DIN|DIN VIEW)", re.I),
    )
    for heading in headings:
        for start, line in enumerate(lines):
            if not heading.search(line.strip()):
                continue
            level = len(line) - len(line.lstrip("#"))
            section = []
            for following in lines[start + 1 :]:
                stripped = following.strip()
                if stripped.startswith("#") and len(stripped) - len(stripped.lstrip("#")) <= level:
                    break
                section.append(following)
            result = "\n".join(section).strip(" \n-\u2014")
            if result:
                return result
    return "Bài viết không có mục kết luận bằng tiếng Việt."


def article_report(metadata: dict, content: str, vietnamese: dict | None = None, translation_error: str = "") -> dict:
    if vietnamese:
        conclusion = vietnamese["conclusion"]
        summary = vietnamese["summary"]
        status = "completed"
        source = "openai"
    elif contains_chinese(content) and source_conclusion(content) == "Bài viết không có mục kết luận bằng tiếng Việt.":
        conclusion = translation_error or "Chưa có kết luận tiếng Việt. Hãy cấu hình OpenAI API và chọn ‘Tạo báo cáo tiếng Việt’."
        summary = ""
        status = "needs_translation"
        source = "article"
    else:
        conclusion = source_conclusion(content)
        summary = ""
        status = "completed"
        source = "article"
    return {
        "metadata": metadata,
        "analysis": {
            "status": status,
            "source": source,
            "translation_error": translation_error,
            "report": {"report_content": content, "summary": summary, "conclusion": conclusion},
        },
    }


def daily_vehicle_counts(raw_root: Path) -> list[dict]:
    """Extract only the independent China land-port totals, not market inventory."""
    records: dict[str, dict] = {}
    section_header = re.compile(r"^#{1,6}\s+(?:中国陆运海关|Hải quan [Đđ]ường bộ Trung Quốc)\s*$", re.I)
    date_pattern = re.compile(r"20\d{2}-\d{2}-\d{2}")
    count_pattern = re.compile(r"\**\s*(\d[\d,.]*)\s*(?:柜|container|cont|xe)\s*\**", re.I)
    summary_pattern = re.compile(r"(?:中国陆运海关|Hải quan [Đđ]ường bộ Trung Quốc)", re.I)
    vietnam_pattern = re.compile(r"(?:越南|Việt Nam)\s*\**\s*(\d[\d,.]*)\s*(?:柜|container|cont|xe)", re.I)
    thailand_pattern = re.compile(r"(?:泰国|Thái Lan)\s*\**\s*(\d[\d,.]*)\s*(?:柜|container|cont|xe)", re.I)
    labels = {"越南": "vietnam", "Việt Nam": "vietnam", "泰国": "thailand", "Thái Lan": "thailand", "泰越合计": "total", "Tổng cộng": "total"}

    for path in raw_root.glob("*/*/*/*/*/article.md"):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = content.splitlines()
        candidates = []
        for start, line in enumerate(lines):
            if not section_header.match(line.strip()):
                continue
            window = lines[start + 1 : start + 35]
            date = next((match.group() for row in window[:8] if (match := date_pattern.search(row))), None)
            if not date:
                continue
            values = {}
            evidence_rows = []
            for row in window:
                if not row.startswith("|"):
                    continue
                cells = [cell.strip().strip("*") for cell in row.strip("| ").split("|")]
                if len(cells) < 2:
                    continue
                key = labels.get(cells[0])
                match = count_pattern.search(row)
                if key and match:
                    values[key] = int(match.group(1).replace(",", ""))
                    evidence_rows.append(row.strip())
            if "vietnam" not in values or "thailand" not in values:
                continue
            calculated = values["vietnam"] + values["thailand"]
            if values.get("total", calculated) != calculated:
                continue
            candidates.append((date, values, "\n".join(evidence_rows)))
            break
        if not candidates:
            for line in lines:
                if not summary_pattern.search(line):
                    continue
                date_match = date_pattern.search(line)
                vietnam_match = vietnam_pattern.search(line)
                thailand_match = thailand_pattern.search(line)
                if date_match and vietnam_match and thailand_match:
                    candidates.append((date_match.group(), {
                        "vietnam": int(vietnam_match.group(1).replace(",", "")),
                        "thailand": int(thailand_match.group(1).replace(",", "")),
                    }, line.strip()))
                    break
        for date, values, evidence in candidates:
            calculated = values["vietnam"] + values["thailand"]
            metadata_path = path.with_name("metadata.json")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            record = {
                "date": date,
                "vietnam": values["vietnam"],
                "thailand": values["thailand"],
                "total": calculated,
                "unit": "container",
                "source_title": metadata.get("title", ""),
                "source_url": metadata.get("url", ""),
                "source_id": "/".join(path.parent.relative_to(raw_root).parts),
                "source_excerpt": evidence,
            }
            if date not in records or path.stat().st_mtime > records[date]["_mtime"]:
                record["_mtime"] = path.stat().st_mtime
                records[date] = record
    return [{key: value for key, value in record.items() if key != "_mtime"} for _, record in sorted(records.items(), reverse=True)]
