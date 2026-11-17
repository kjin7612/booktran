import os

import pymupdf
import pytest

from app.pdf_translate import extract_blocks, translate_pdf
from app.providers.fake import FakeProvider

FONT = os.environ.get("BOOK_TRAN_FONT", r"C:\Windows\Fonts\malgun.ttf")
needs_font = pytest.mark.skipif(not os.path.exists(FONT), reason="한글 폰트가 없습니다")


def make_pdf(path):
    """테스트용 텍스트 PDF. 직접 만든 짧은 문장만 쓴다."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Test Heading", fontsize=20)
    page.insert_textbox(
        pymupdf.Rect(72, 110, 400, 220),
        "This generated paragraph checks the text based PDF translation pipeline.",
        fontsize=11,
    )
    page.insert_text((300, 800), "1", fontsize=9)  # 쪽 번호는 번역 대상이 아니다
    doc.new_page().insert_textbox(
        pymupdf.Rect(72, 72, 520, 200), "Second page with a hyphen-\nated word.", fontsize=11
    )
    doc.save(str(path))
    doc.close()
    return path


def test_extract_skips_page_numbers_and_joins_hyphens(tmp_path):
    src = make_pdf(tmp_path / "in.pdf")
    doc = pymupdf.open(str(src))
    try:
        blocks = extract_blocks(doc)
    finally:
        doc.close()
    texts = [b.text for b in blocks]
    assert "1" not in texts
    assert any("hyphenated" in t for t in texts)
    assert any(abs(b.size - 20.0) < 0.5 for b in blocks)


@needs_font
def test_translate_pdf_keeps_pages_and_writes_translation(tmp_path):
    src = make_pdf(tmp_path / "in.pdf")
    dst = tmp_path / "out.pdf"
    report = translate_pdf(src, dst, FakeProvider(), model_id="fake")

    assert report.failed == []
    assert report.translated == report.total > 0
    out = pymupdf.open(str(dst))
    try:
        assert len(out) == 2
        assert "[KO]" in out[0].get_text()
    finally:
        out.close()


@needs_font
def test_pdf_without_text_is_rejected(tmp_path):
    doc = pymupdf.open()
    doc.new_page()  # 글자가 없는 빈 쪽
    src = tmp_path / "blank.pdf"
    doc.save(str(src))
    doc.close()
    with pytest.raises(ValueError):
        translate_pdf(src, tmp_path / "out.pdf", FakeProvider(), model_id="fake")
