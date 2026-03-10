from __future__ import annotations

import os
from typing import List

import pytesseract
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError
from pydantic import BaseModel, Field


app = FastAPI(
    title="PDF OCR API",
    description="Microservicio para extraer texto de PDFs con Tesseract OCR.",
    version="1.0.0",
)

DEFAULT_LANG = os.getenv("OCR_DEFAULT_LANG", "spa+eng")
DEFAULT_DPI = int(os.getenv("OCR_DEFAULT_DPI", "300"))
DEFAULT_TESS_CONFIG = os.getenv("OCR_DEFAULT_TESS_CONFIG", "--oem 3 --psm 6")
MAX_FILE_SIZE_MB = int(os.getenv("OCR_MAX_FILE_SIZE_MB", "30"))
TESSERACT_CMD = os.getenv("TESSERACT_CMD")

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


class OCRPageResult(BaseModel):
    page: int = Field(..., description="Numero de pagina (base 1).")
    text: str = Field(..., description="Texto OCR de la pagina.")


class OCRPdfResponse(BaseModel):
    filename: str = Field(..., description="Nombre del archivo procesado.")
    language: str = Field(..., description="Idioma(s) usados por Tesseract.")
    pages: int = Field(..., description="Total de paginas procesadas.")
    results: List[OCRPageResult] = Field(..., description="Resultado por pagina.")
    full_text: str = Field(..., description="Texto completo concatenado.")


def _ocr_pdf(
    pdf_bytes: bytes,
    lang: str,
    dpi: int,
    tess_config: str,
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
        text = pytesseract.image_to_string(image, lang=lang, config=tess_config).strip()
        page_results.append(OCRPageResult(page=page_number, text=text))

    full_text = "\n\n".join(page.text for page in page_results).strip()
    return OCRPdfResponse(
        filename="",
        language=lang,
        pages=len(page_results),
        results=page_results,
        full_text=full_text,
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

        result = await run_in_threadpool(_ocr_pdf, content, lang, dpi, config, first_page, last_page)
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
