FROM python:3.12-slim

# calibre trae ebook-convert; imagemagick + ghostscript renderizan PDFs como imagen.
RUN apt-get update && apt-get install -y --no-install-recommends \
        calibre \
        imagemagick \
        ghostscript \
        libxcb-cursor0 \
    && rm -rf /var/lib/apt/lists/*

# Debian deshabilita por defecto que ImageMagick lea/escriba PDF (CVE viejo).
# Sin esto, "magick ... entrada.pdf ... salida.jpg" falla con "not authorized".
RUN for f in /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml; do \
        [ -f "$f" ] && sed -i 's/rights="none" pattern="PDF"/rights="read|write" pattern="PDF"/' "$f"; \
    done; true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py conversor.py index.html ./

ENV PYTHONUNBUFFERED=1
# La salida a PDF de Calibre renderiza con QtWebEngine (un Chromium
# empotrado). Ese Chromium se niega a arrancar como root sin --no-sandbox,
# y el contenedor no tiene GPU, asi que ademas hay que forzar software
# rendering. Sin esto, doc-a-pdf falla con "Running as root without
# --no-sandbox is not supported".
ENV QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage"
EXPOSE 8000

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
