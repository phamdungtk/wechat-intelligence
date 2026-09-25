import os
import json
import httpx
from bs4 import BeautifulSoup
from datetime import datetime
from uuid import uuid4
from loguru import logger
from markdownify import markdownify as to_markdown

def download_article(url: str, base_storage_dir: str = "storage/raw"):
    logger.info(f"Downloading article: {url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = httpx.get(url, headers=headers, timeout=15.0, follow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            challenge_path = os.path.join(base_storage_dir, "..", "captcha.json")
            with open(challenge_path, "w", encoding="utf-8") as f:
                json.dump({"article_url": url, "captcha_url": location, "created_at": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
            logger.warning(f"WeChat yêu cầu CAPTCHA cho bài: {url}")
            return None
        response.raise_for_status()
        html_content = response.text
        captcha_path = os.path.join(base_storage_dir, "..", "captcha.json")
        if os.path.exists(captcha_path):
            os.remove(captcha_path)
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(html_content, 'html.parser')

    # Bóc tách Title (Tiêu đề bài viết)
    title_tag = soup.find('h1', class_='rich_media_title')
    title = title_tag.text.strip() if title_tag else "Unknown_Title"

    # Bóc tách Tên OA (Tác giả)
    account_tag = soup.find('strong', class_='profile_nickname')
    if not account_tag:
        account_tag = soup.find('a', id='js_name')
    account_name = account_tag.text.strip() if account_tag else "Unknown_Account"
    if title == "Unknown_Title" or account_name == "Unknown_Account":
        logger.warning("Trang trả về không phải bài viết WeChat hợp lệ")
        return None
    article = soup.find(id='js_content') or soup
    image_urls = []
    for image in article.find_all('img'):
        image_url = image.get('data-src') or image.get('data-original') or image.get('src')
        if image_url and image_url.startswith(('http://', 'https://')) and image_url not in image_urls:
            image_urls.append(image_url)
            image['src'] = image_url

    # Tạo thư mục riêng cho từng lần tải để không ghi đè bài cũ.
    now = datetime.now()
    safe_account_name = "".join(c for c in account_name if c.isalnum() or c in (' ', '-', '_')).strip()
    if not safe_account_name:
        safe_account_name = "Unknown_Account"

    account_dir = os.path.abspath(os.path.join(base_storage_dir, safe_account_name))
    storage_root = os.path.abspath(base_storage_dir)
    if os.path.dirname(account_dir) != storage_root:
        raise ValueError("Invalid storage path")

    dir_path = os.path.join(
        base_storage_dir,
        safe_account_name,
        str(now.year), f"{now.month:02d}", f"{now.day:02d}",
        f"{now.strftime('%H%M%S%f')}-{uuid4().hex}"
    )
    os.makedirs(dir_path, exist_ok=False)

    # 1. Lưu file HTML gốc
    html_path = os.path.join(dir_path, "article.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    # 2. Chuyển riêng nội dung bài viết sang Markdown, giữ heading, list, bảng và ảnh.
    for script in article(["script", "style"]):
        script.extract()
    markdown_content = to_markdown(str(article), heading_style="ATX", bullets="-").strip()
    markdown_path = os.path.join(dir_path, "article.md")
    with open(markdown_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content + "\n")

    # 3. Lưu Metadata (JSON)
    metadata = {
        "title": title,
        "account_name": account_name,
        "url": url,
        "crawled_at": now.isoformat(),
        "status": "downloaded"
        ,"image_urls": image_urls[:30]
    }
    meta_path = os.path.join(dir_path, "metadata.json")
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=4)

    logger.success(f"Tải thành công! Dữ liệu gốc lưu tại: {dir_path}")
    return dir_path
