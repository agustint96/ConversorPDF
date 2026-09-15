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
COPY static ./static

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
