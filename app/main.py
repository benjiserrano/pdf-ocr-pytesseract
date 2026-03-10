from __future__ import annotations

import os
import re
import unicodedata
from typing import List

import pytesseract
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError
from PIL import Image, ImageFilter, ImageOps
from pydantic import BaseModel, Field


app = FastAPI(
    title="PDF OCR API",
    description="Microservicio para extraer texto de PDFs con Tesseract OCR.",
    version="1.0.0",
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}

DEFAULT_LANG = os.getenv("OCR_DEFAULT_LANG", "spa+eng")
DEFAULT_DPI = int(os.getenv("OCR_DEFAULT_DPI", "300"))
DEFAULT_TESS_CONFIG = os.getenv("OCR_DEFAULT_TESS_CONFIG", "--oem 3 --psm 6")
MAX_FILE_SIZE_MB = int(os.getenv("OCR_MAX_FILE_SIZE_MB", "30"))
DEFAULT_PREPROCESS_IMAGE = _env_bool("OCR_PREPROCESS_IMAGE", True)
DEFAULT_NORMALIZE_FOR_RAG = _env_bool("OCR_NORMALIZE_FOR_RAG", True)
TESSERACT_CMD = os.getenv("TESSERACT_CMD")

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


class OCRPageResult(BaseModel):
    page: int = Field(..., description="Numero de pagina (base 1).")
    text: str = Field(..., description="Texto OCR de la pagina.")
    clean_text: str = Field(..., description="Texto OCR normalizado para indexacion/RAG.")


class OCRPdfResponse(BaseModel):
    filename: str = Field(..., description="Nombre del archivo procesado.")
    language: str = Field(..., description="Idioma(s) usados por Tesseract.")
    pages: int = Field(..., description="Total de paginas procesadas.")
    results: List[OCRPageResult] = Field(..., description="Resultado por pagina.")
    full_text: str = Field(..., description="Texto completo concatenado.")
    full_text_clean: str = Field(..., description="Texto completo normalizado para indexacion/RAG.")


def _preprocess_image_for_ocr(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    bw = gray.point(lambda px: 255 if px > 180 else 0, mode="1")
    return bw.convert("L")


def _clean_ocr_text_for_rag(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    lines = normalized.splitlines()
    cleaned_lines: List[str] = []

    for line in lines:
        line = line.replace("\t", " ").strip()
        if not line:
            cleaned_lines.append("")
            continue

        line = re.sub(r"[\.·•]{4,}", " ", line)
        line = re.sub(r"\b([A-Za-zÁÉÍÓÚÜÑáéíóúüñ])\1{3,}\b", r"\1", line)
        line = re.sub(r"\s{2,}", " ", line).strip()

        if re.fullmatch(r"[-_=~]{3,}", line):
            continue

        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _ocr_pdf(
    pdf_bytes: bytes,
    lang: str,
    dpi: int,
    tess_config: str,
    preprocess_image: bool = True,
    normalize_for_rag: bool = True,
    first_page: int | None = None,
    last_page: int | None = None,
) -> OCRPdfResponse:
    convert_kwargs = {"dpi": dpi, "fmt": "png"}
    if first_page is not None:
        convert_kwargs["first_page"] = first_page
    if last_page is not None:
        convert_kwargs["last_page"] = last_page

    images = convert_from_bytes(pdf_bytes, **convert_kwargs)

    page_results: List[OCRPageResult] = []
    page_offset = first_page if first_page is not None else 1
    for index, image in enumerate(images):
        page_number = page_offset + index
        image_for_ocr = _preprocess_image_for_ocr(image) if preprocess_image else image
        text = pytesseract.image_to_string(image_for_ocr, lang=lang, config=tess_config).strip()
        clean_text = _clean_ocr_text_for_rag(text) if normalize_for_rag else text
        page_results.append(OCRPageResult(page=page_number, text=text, clean_text=clean_text))

    full_text = "\n\n".join(page.text for page in page_results).strip()
    full_text_clean = "\n\n".join(page.clean_text for page in page_results).strip()
    return OCRPdfResponse(
        filename="",
        language=lang,
        pages=len(page_results),
        results=page_results,
        full_text=full_text,
        full_text_clean=full_text_clean,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "pdf-ocr-api"}


@app.post("/ocr/pdf", response_model=OCRPdfResponse)
async def ocr_pdf(
    file: UploadFile = File(..., description="Archivo PDF a procesar."),
    lang: str = Query(DEFAULT_LANG, description="Idioma(s) Tesseract. Ej: spa, eng, spa+eng."),
    dpi: int = Query(DEFAULT_DPI, ge=100, le=600, description="Resolucion para rasterizar paginas PDF."),
    config: str = Query(DEFAULT_TESS_CONFIG, description="Config adicional para Tesseract."),
    preprocess_image: bool = Query(
        DEFAULT_PREPROCESS_IMAGE,
        description="Aplica limpieza de imagen previa al OCR (recomendado para PDFs escaneados).",
    ),
    normalize_for_rag: bool = Query(
        DEFAULT_NORMALIZE_FOR_RAG,
        description="Normaliza ruido OCR (lineas de puntos y repeticiones) para indexacion/RAG.",
    ),
    first_page: int | None = Query(None, ge=1, description="Primera pagina a procesar (base 1)."),
    last_page: int | None = Query(None, ge=1, description="Ultima pagina a procesar (base 1)."),
) -> OCRPdfResponse:
    max_size_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    filename = file.filename or "document.pdf"

    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="El archivo PDF esta vacio.")

        if len(content) > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"El archivo excede el limite permitido de {MAX_FILE_SIZE_MB} MB.",
            )

        content_type = file.content_type or ""
        if content_type not in {"application/pdf", "application/octet-stream", "application/x-pdf"}:
            raise HTTPException(status_code=400, detail="El archivo debe ser un PDF.")

        if first_page and last_page and last_page < first_page:
            raise HTTPException(
                status_code=400,
                detail="`last_page` no puede ser menor que `first_page`.",
            )

        result = await run_in_threadpool(
            _ocr_pdf,
            content,
            lang,
            dpi,
            config,
            preprocess_image,
            normalize_for_rag,
            first_page,
            last_page,
        )
        result.filename = filename
        return result

    except HTTPException:
        raise
    except (PDFPageCountError, PDFSyntaxError):
        raise HTTPException(status_code=400, detail="PDF invalido o corrupto.")
    except PDFInfoNotInstalledError:
        raise HTTPException(
            status_code=500,
            detail="Poppler no esta disponible en el entorno de ejecucion.",
        )
    except pytesseract.pytesseract.TesseractError as err:
        raise HTTPException(status_code=422, detail=f"Error de Tesseract: {err}")
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Error inesperado: {err}")
    finally:
        await file.close()
