import sys
import argparse
from app.downloader.downloader import download_article

def main():
    parser = argparse.ArgumentParser(description="Test WeChat Downloader")
    parser.add_argument("url", nargs="?", default="", help="WeChat Article URL")
    args = parser.parse_args()

    url = args.url
    if not url:
        print("Lỗi: Vui lòng cung cấp link bài viết WeChat!")
        print("Ví dụ: python poc.py \"https://mp.weixin.qq.com/s/xxx\"")
        sys.exit(1)

    print("=" * 50)
    print(" BAT DAU CHAY THU NGHIEM TAI BAI VIET (PHASE 1)")
    print("=" * 50)

    saved_dir = download_article(url, base_storage_dir="storage/raw")

    if saved_dir:
        print("\n=> KET QUA: TAI THANH CONG!")
        print(f"=> Duong dan thu muc chua bai viet: {saved_dir}")
        print("=> Hay kiem tra file article.html, article.md va metadata.json ben trong.")
    else:
        print("\n=> KET QUA: TAI THAT BAI!")

if __name__ == "__main__":
    main()
