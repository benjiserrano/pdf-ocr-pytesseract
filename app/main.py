from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from typing import List

import pytesseract
from pytesseract import Output
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
DEFAULT_STRIP_REPEATED_LINES = _env_bool("OCR_STRIP_REPEATED_LINES", True)
DEFAULT_AUTO_DETECT_LANG = _env_bool("OCR_AUTO_DETECT_LANG", False)
DEFAULT_LANG_CANDIDATES = os.getenv(
    "OCR_LANG_CANDIDATES",
    "spa,cat,eus,eng,spa+eng,cat+spa,eus+spa",
)
DEFAULT_LANG_SAMPLE_PAGES = int(os.getenv("OCR_LANG_SAMPLE_PAGES", "2"))
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
    detected_language: str = Field(..., description="Idioma OCR realmente utilizado tras resolución automática/manual.")
    auto_detected_language: bool = Field(..., description="Indica si el idioma se detectó automáticamente.")


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
    cleaned = re.sub(r"-\n([a-záéíóúüñ])", r"\1", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _parse_lang_candidates(raw: str) -> List[str]:
    candidates = [part.strip() for part in raw.split(",") if part.strip()]
    deduped: List[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def _score_language_for_image(image: Image.Image, lang: str, tess_config: str) -> float:
    try:
        data = pytesseract.image_to_data(image, lang=lang, config=tess_config, output_type=Output.DICT)
    except pytesseract.pytesseract.TesseractError:
        return -1e9

    confidences: List[float] = []
    text_parts: List[str] = []
    for conf_raw, token in zip(data.get("conf", []), data.get("text", [])):
        token = (token or "").strip()
        if not token:
            continue
        text_parts.append(token)
        try:
            conf_val = float(conf_raw)
            if conf_val >= 0:
                confidences.append(conf_val)
        except (TypeError, ValueError):
            continue

    if not text_parts:
        return -1e9

    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    joined = " ".join(text_parts)
    alpha_chars = sum(1 for ch in joined if ch.isalpha())
    non_space_chars = sum(1 for ch in joined if not ch.isspace())
    alpha_ratio = alpha_chars / non_space_chars if non_space_chars else 0.0

    return avg_conf + (alpha_ratio * 20.0)


def _resolve_ocr_language(
    images_for_scoring: List[Image.Image],
    fallback_lang: str,
    tess_config: str,
    auto_detect_lang: bool,
    lang_candidates: List[str],
    lang_sample_pages: int,
) -> tuple[str, bool]:
    if not auto_detect_lang:
        return fallback_lang, False

    if not lang_candidates:
        return fallback_lang, False

    sample_count = max(1, min(lang_sample_pages, len(images_for_scoring)))
    sample_images = images_for_scoring[:sample_count]

    best_lang = fallback_lang
    best_score = -1e9

    for candidate in lang_candidates:
        scores = [_score_language_for_image(img, candidate, tess_config) for img in sample_images]
        valid_scores = [score for score in scores if score > -1e8]
        if not valid_scores:
            continue
        avg_score = sum(valid_scores) / len(valid_scores)
        if avg_score > best_score:
            best_score = avg_score
            best_lang = candidate

    return best_lang, best_lang != fallback_lang


def _line_signature_for_dedup(line: str) -> str:
    line = unicodedata.normalize("NFD", line)
    line = "".join(ch for ch in line if unicodedata.category(ch) != "Mn")
    line = line.lower()
    line = re.sub(r"\d+", " ", line)
    line = re.sub(r"[^a-zñ\s]", " ", line)
    line = re.sub(r"\s{2,}", " ", line).strip()
    return line


def _strip_repeated_structural_lines(page_texts: List[str]) -> List[str]:
    if len(page_texts) < 3:
        return page_texts

    top_n = 7
    bottom_n = 7
    min_ratio = 0.5
    min_len = 10

    signature_counter: Counter[str] = Counter()
    threshold = max(2, int(len(page_texts) * min_ratio))

    page_lines_cache: List[List[str]] = []
    for text in page_texts:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        page_lines_cache.append(lines)
        candidate_lines = lines[:top_n] + lines[-bottom_n:]
        seen_signatures: set[str] = set()
        for line in candidate_lines:
            signature = _line_signature_for_dedup(line)
            if len(signature) < min_len or signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            signature_counter[signature] += 1

    repeated_signatures = {
        signature
        for signature, count in signature_counter.items()
        if count >= threshold
    }

    if not repeated_signatures:
        return page_texts

    cleaned_pages: List[str] = []
    for lines in page_lines_cache:
        kept: List[str] = []
        for line in lines:
            signature = _line_signature_for_dedup(line)
            if signature in repeated_signatures and len(signature) >= min_len:
                continue
            kept.append(line)
        cleaned_pages.append("\n".join(kept).strip())

    return cleaned_pages


def _ocr_pdf(
    pdf_bytes: bytes,
    lang: str,
    dpi: int,
    tess_config: str,
    preprocess_image: bool = True,
    normalize_for_rag: bool = True,
    strip_repeated_lines: bool = True,
    auto_detect_lang: bool = False,
    lang_candidates_raw: str = "",
    lang_sample_pages: int = 2,
    first_page: int | None = None,
    last_page: int | None = None,
) -> OCRPdfResponse:
    convert_kwargs = {"dpi": dpi, "fmt": "png"}
    if first_page is not None:
        convert_kwargs["first_page"] = first_page
    if last_page is not None:
        convert_kwargs["last_page"] = last_page

    images = convert_from_bytes(pdf_bytes, **convert_kwargs)

    processed_images = [_preprocess_image_for_ocr(img) if preprocess_image else img for img in images]
    lang_candidates = _parse_lang_candidates(lang_candidates_raw)
    resolved_lang, auto_detected_language = _resolve_ocr_language(
        images_for_scoring=processed_images,
        fallback_lang=lang,
        tess_config=tess_config,
        auto_detect_lang=auto_detect_lang,
        lang_candidates=lang_candidates,
        lang_sample_pages=lang_sample_pages,
    )

    page_results: List[OCRPageResult] = []
    page_offset = first_page if first_page is not None else 1
    for index, image_for_ocr in enumerate(processed_images):
        page_number = page_offset + index
        text = pytesseract.image_to_string(image_for_ocr, lang=resolved_lang, config=tess_config).strip()
        clean_text = _clean_ocr_text_for_rag(text) if normalize_for_rag else text
        page_results.append(OCRPageResult(page=page_number, text=text, clean_text=clean_text))

    if normalize_for_rag and strip_repeated_lines:
        deduped_pages = _strip_repeated_structural_lines([page.clean_text for page in page_results])
        for page, deduped_text in zip(page_results, deduped_pages):
            page.clean_text = deduped_text

    full_text = "\n\n".join(page.text for page in page_results).strip()
    full_text_clean = "\n\n".join(page.clean_text for page in page_results).strip()
    return OCRPdfResponse(
        filename="",
        language=resolved_lang,
        pages=len(page_results),
        results=page_results,
        full_text=full_text,
        full_text_clean=full_text_clean,
        detected_language=resolved_lang,
        auto_detected_language=auto_detected_language,
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
    strip_repeated_lines: bool = Query(
        DEFAULT_STRIP_REPEATED_LINES,
        description="Elimina cabeceras/pies repetidos entre paginas en la salida limpia.",
    ),
    auto_detect_lang: bool = Query(
        DEFAULT_AUTO_DETECT_LANG,
        description="Autodetecta idioma OCR usando candidatos y confianza de reconocimiento.",
    ),
    lang_candidates: str = Query(
        DEFAULT_LANG_CANDIDATES,
        description="Lista CSV de idiomas candidatos para autodetección. Ej: spa,cat,eus,eng,spa+eng",
    ),
    lang_sample_pages: int = Query(
        DEFAULT_LANG_SAMPLE_PAGES,
        ge=1,
        le=5,
        description="Numero de paginas iniciales para detectar idioma automaticamente.",
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
            strip_repeated_lines,
            auto_detect_lang,
            lang_candidates,
            lang_sample_pages,
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
