# WeChatPlayer converter API.
#
# Two stages: the Python dependencies are installed into an isolated prefix in
# the builder, then only that prefix is copied into the runtime image, so build
# tooling and pip caches never ship.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

# LibreOffice and FFmpeg are the external programs the conversion endpoints
# shell out to.  libreoffice-writer covers every document format this API
# accepts (doc, docx, odt, ott, rtf, txt) and is far smaller than the full
# "libreoffice" metapackage, which also drags in Calc, Impress, Draw and Base.
#
# The CJK fonts are not optional for this project: without them a Chinese
# document converts to a PDF in which every glyph is a blank box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        ffmpeg \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# The service runs LibreOffice and FFmpeg as subprocesses, so it does not run as
# root: a malicious document would otherwise be parsed with full privileges.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin converter

COPY --from=builder /install /usr/local

WORKDIR /app
COPY app ./app

# Per-request conversion directories live here; the runtime user must own it.
RUN mkdir -p /var/lib/wechat-converter/tmp \
    && chown -R converter:converter /var/lib/wechat-converter

USER converter

ENV PYTHONUNBUFFERED=1 \
    CONVERTER_TMP_ROOT=/var/lib/wechat-converter/tmp \
    HOME=/home/converter

EXPOSE 18080

# Liveness only.  /api/v1/health answers 200 even when MySQL is unreachable and
# reports the database state in its body, so this proves the process is serving
# -- not that it can reach its database.  Use the body for a readiness check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18080/api/v1/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18080", "--workers", "1"]
