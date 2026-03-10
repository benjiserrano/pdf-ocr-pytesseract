FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OCR_DEFAULT_LANG=spa+eng \
    OCR_DEFAULT_DPI=300 \
    OCR_DEFAULT_TESS_CONFIG="--oem 3 --psm 6" \
    OCR_MAX_FILE_SIZE_MB=30 \
    OCR_AUTO_DETECT_LANG=false \
    OCR_LANG_CANDIDATES="spa,cat,eus,eng,spa+eng,cat+spa,eus+spa" \
    OCR_LANG_SAMPLE_PAGES=2

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-cat \
    tesseract-ocr-eus \
    tesseract-ocr-eng \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
