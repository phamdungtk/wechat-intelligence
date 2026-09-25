FROM lscr.io/linuxserver/weixin:latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xdotool xclip scrot tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
