"""텍스트 기반 PDF 번역.

원리
- PDF에서 텍스트 블록(위치, 글꼴 크기, 내용)을 뽑는다.
- 블록을 번역 엔진에 보내 번역문을 받는다.
- 원문 블록 영역의 글자만 지우고(이미지와 선은 유지), 같은 자리에 한글 폰트로 번역문을 다시 쓴다.
- 칸보다 길면 글자 크기를 줄이고, 그래도 안 들어가면 overflow로 기록한다.

이미지만 있는 쪽(스캔본)은 텍스트 블록이 없으므로 원본 그대로 둔다.
"""

from __future__ import annotations

import os
import re
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .epub_translate import TranslationCache, TranslationProvider, translate_units

DEFAULT_FONT = r"C:\Windows\Fonts\malgun.ttf"
LINE_SPACING = 1.25
MIN_SCALE = 0.7  # 원래 크기의 70%까지만 줄인다


def font_path() -> str:
    return os.environ.get("BOOK_TRAN_FONT", DEFAULT_FONT)


@dataclass
class TextBlock:
    page: int
    index: int
    text: str
    rect: tuple[float, float, float, float]
    size: float


@dataclass
class PdfUnit:
    key: str
    marked: str  # translate_units 호환을 위해 이름을 맞춘다(PDF는 태그를 쓰지 않는다)


@dataclass
class PdfReport:
    pages: int
    pages_without_text: int
    total: int
    translated: int
    cached: int
    failed: list[str]
    overflow: int
    alignments: list[dict]


def _join_lines(lines: list[str]) -> str:
    out = lines[0]
    for nxt in lines[1:]:
        if out.endswith("-") and nxt[:1].islower():  # 줄 끝 하이픈 붙이기
            out = out[:-1] + nxt
        else:
            out = out + " " + nxt
    return out


def extract_blocks(doc: pymupdf.Document) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for pno, page in enumerate(doc):
        index = 0
        for raw in page.get_text("dict")["blocks"]:
            if raw.get("type") != 0:  # 0 = 텍스트, 1 = 이미지
                continue
            lines: list[str] = []
            sizes: list[float] = []
            for line in raw["lines"]:
                spans = line["spans"]
                text = "".join(s["text"] for s in spans).strip()
                if text:
                    lines.append(text)
                sizes += [s["size"] for s in spans if s["text"].strip()]
            if not lines or not sizes:
                continue
            text = _join_lines(lines)
            if not any(ch.isalpha() for ch in text):  # 쪽 번호, 기호만 있는 블록은 제외
                continue
            blocks.append(TextBlock(
                page=pno,
                index=index,
                text=text,
                rect=tuple(raw["bbox"]),
                size=float(statistics.median(sizes)),
            ))
            index += 1
    return blocks


def wrap(text: str, font: pymupdf.Font, size: float, width: float) -> list[str]:
    """글자 단위로 줄을 나눈다. 공백이 있으면 공백에서 끊는다(한글과 영문 모두 동작)."""
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if not cur or font.text_length(trial, fontsize=size) <= width:
            cur = trial
            continue
        pos = cur.rfind(" ")
        if pos > 0 and ch != " ":
            lines.append(cur[:pos])
            cur = cur[pos + 1:] + ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return [line.strip() for line in lines if line.strip()]


def layout(
    text: str, font: pymupdf.Font, width: float, height: float, size: float
) -> tuple[list[str], float, bool]:
    """(줄 목록, 사용할 글자 크기, 칸에 들어갔는지)."""
    min_size = max(5.0, size * MIN_SCALE)
    scale = size
    while scale >= min_size:
        lines = wrap(text, font, scale, width)
        if len(lines) * scale * LINE_SPACING <= height + 1:
            return lines, scale, True
        scale -= 0.5
    lines = wrap(text, font, min_size, width)
    return lines, min_size, False


def translate_pdf(
    src: str | Path,
    dst: str | Path,
    provider: TranslationProvider,
    target_lang: str = "ko",
    cache: TranslationCache | None = None,
    model_id: str | None = None,
    concurrency: int = 3,
    on_progress=None,
    on_error=None,
    sleep=time.sleep,
) -> PdfReport:
    model_id = model_id or provider.name
    font_file = font_path()
    doc = pymupdf.open(str(src))
    try:
        blocks = extract_blocks(doc)
        pages_with_text = {b.page for b in blocks}
        if not blocks:
            raise ValueError("텍스트가 없는 PDF입니다(스캔본 등). 텍스트가 있는 PDF만 지원합니다.")

        units = [PdfUnit(key=f"{b.page}#{b.index}", marked=b.text) for b in blocks]
        results, cached_keys = translate_units(
            units, provider, target_lang, model_id, cache,
            concurrency=concurrency, on_progress=on_progress, on_error=on_error, sleep=sleep,
        )

        by_page: dict[int, list[tuple[TextBlock, str]]] = defaultdict(list)
        alignments: list[dict] = []
        failed: list[str] = []
        translated_count = 0
        for block, unit in zip(blocks, units):
            value = results.get(unit.key)
            ok = isinstance(value, str) and value.strip() != ""
            if ok:
                by_page[block.page].append((block, value.strip()))
                translated_count += 1
                if cache is not None and unit.key not in cached_keys:
                    cache.put(cache_key_for(model_id, target_lang, unit.marked), value.strip())
            else:
                failed.append(unit.key)
            alignments.append({
                "page": block.page,
                "index": block.index,
                "original": block.text,
                "translated": value.strip() if ok else None,
                "status": "translated" if ok else "failed",
            })

        overflow = 0
        font = pymupdf.Font(fontfile=font_file)
        for pno, items in by_page.items():
            page = doc[pno]
            rects = [pymupdf.Rect(b.rect) for b, _ in items]
            for rect in rects:
                page.add_redact_annot(rect, fill=(1, 1, 1))
            # 이미지와 선은 그대로 두고 글자만 지운다
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            )
            for block, value in items:
                rect = pymupdf.Rect(block.rect)
                lines, scale, fits = layout(value, font, rect.width, rect.height, block.size)
                if not fits:
                    overflow += 1
                y = rect.y0 + scale
                for line in lines:
                    page.insert_text(
                        pymupdf.Point(rect.x0, y), line,
                        fontname="ko", fontfile=font_file, fontsize=scale, color=(0, 0, 0),
                    )
                    y += scale * LINE_SPACING

        doc.save(str(dst), garbage=3, deflate=True)
        return PdfReport(
            pages=len(doc),
            pages_without_text=len(doc) - len(pages_with_text),
            total=len(blocks),
            translated=translated_count,
            cached=len(cached_keys),
            failed=failed,
            overflow=overflow,
            alignments=alignments,
        )
    finally:
        doc.close()


def cache_key_for(model_id: str, target_lang: str, text: str) -> str:
    from .epub_translate import cache_key

    return cache_key(model_id, target_lang, text)
