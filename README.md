# PDF OCR API (FastAPI + Tesseract)

Microservicio para extraer texto de archivos PDF usando OCR con `pytesseract`.

Incluye modo escalable para licitaciones multilingües (castellano/catalán/euskera/inglés)
con autodetección opcional de idioma OCR.

## 1) Ejecutar con Docker

### Build
```bash
docker build -t pdf-ocr-api:latest .
```

### Run
```bash
docker run -d \
  --name pdf-ocr-api \
  -p 8000:8000 \
  -e OCR_DEFAULT_LANG=spa+eng \
  -e OCR_MAX_FILE_SIZE_MB=30 \
  --restart unless-stopped \
  pdf-ocr-api:latest
```

### O con Docker Compose
```bash
docker compose up -d --build
```

## 2) Probar la API

### Healthcheck
```bash
curl http://localhost:8000/health
```

### OCR de PDF
```bash
curl -X POST "http://localhost:8000/ocr/pdf?lang=spa+eng&dpi=300&first_page=1&last_page=2" \
  -H "accept: application/json" \
  -F "file=@/ruta/a/documento.pdf"
```

Respuesta (ejemplo):
```json
{
  "filename": "documento.pdf",
  "language": "spa+eng",
  "pages": 2,
  "detected_language": "spa+eng",
  "auto_detected_language": false,
  "results": [
    {"page": 1, "text": "Texto de la pagina 1", "clean_text": "Texto limpio para RAG pagina 1"},
    {"page": 2, "text": "Texto de la pagina 2", "clean_text": "Texto limpio para RAG pagina 2"}
  ],
  "full_text": "Texto de la pagina 1\n\nTexto de la pagina 2",
  "full_text_clean": "Texto limpio para RAG pagina 1\n\nTexto limpio para RAG pagina 2"
}
```

## 3) Variables de entorno

- `OCR_DEFAULT_LANG` (default: `spa+eng`)
- `OCR_DEFAULT_DPI` (default: `300`)
- `OCR_DEFAULT_TESS_CONFIG` (default: `--oem 3 --psm 6`)
- `OCR_MAX_FILE_SIZE_MB` (default: `30`)
- `OCR_PREPROCESS_IMAGE` (default: `true`) preprocesado de imagen antes de OCR
- `OCR_NORMALIZE_FOR_RAG` (default: `true`) limpieza de ruido OCR para indexación
- `OCR_STRIP_REPEATED_LINES` (default: `true`) elimina cabeceras/pies repetidos entre páginas
- `OCR_AUTO_DETECT_LANG` (default: `false`) activa autodetección de idioma OCR
- `OCR_LANG_CANDIDATES` (default: `spa,cat,eus,eng,spa+eng,cat+spa,eus+spa`) candidatos para autodetección
- `OCR_LANG_SAMPLE_PAGES` (default: `2`) páginas iniciales usadas para detectar idioma
- `TESSERACT_CMD` (opcional, ruta custom al binario `tesseract`)

## 4) Endpoints

- `GET /health`
- `POST /ocr/pdf`

Parámetros de query de `POST /ocr/pdf`:
- `lang`: idioma o combinación (`spa`, `eng`, `spa+eng`)
- `dpi`: 100-600
- `config`: parámetros extra para Tesseract
- `preprocess_image`: aplica limpieza de imagen previa (true/false)
- `normalize_for_rag`: limpia artefactos típicos OCR para RAG (true/false)
- `strip_repeated_lines`: elimina líneas repetidas de cabecera/pie en la salida limpia (true/false)
- `auto_detect_lang`: autodetecta el idioma OCR según confianza (true/false)
- `lang_candidates`: CSV de idiomas candidatos para autodetección
- `lang_sample_pages`: número de páginas iniciales para detección de idioma (1-5)
- `first_page`: primera página a procesar (opcional)
- `last_page`: última página a procesar (opcional)

Archivo multipart:
- `file`: PDF

## 5) Recomendaciones para distintas regiones/licencias

- Si el origen es heterogéneo (Castilla + Euskadi + Cataluña), activa `auto_detect_lang=true`.
- Si conoces el idioma por fuente, usa modo manual (`lang=...`) para máxima velocidad.
- Para pliegos mixtos, prioriza combinaciones en candidatos (`cat+spa`, `eus+spa`) para reducir errores.
- Para RAG, indexa `full_text_clean` y conserva `full_text` para trazabilidad.

## 6) Despliegue recomendado en VPS

1. Abrir el puerto `8000` o publicarlo detrás de Nginx/Caddy.
2. Usar `docker compose up -d --build`.
3. Añadir TLS con proxy reverso (Let's Encrypt).
4. Configurar límites de subida en proxy (si procesas PDFs grandes).
