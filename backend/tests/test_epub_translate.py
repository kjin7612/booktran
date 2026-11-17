import zipfile
from pathlib import Path

import pytest
from lxml import etree

from app.epub_translate import (
    MarkupError,
    TranslationCache,
    apply_translation,
    find_leaf_blocks,
    rebuild_block,
    serialize_block,
    translate_epub,
)
from app.providers.fake import FakeProvider

XHTML_NS = "http://www.w3.org/1999/xhtml"

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
<head><title>Ch1</title><style>p { color: red; }</style></head>
<body>
<h1>Chapter One</h1>
<p>Plain paragraph with <em>emphasis</em> and a <a href="ch2.xhtml#s2">link</a>.</p>
<p>Image: <img src="../Images/cover.png" alt="cover"/> after image.</p>
<ul><li>Item <strong>bold</strong></li></ul>
<pre><code>x = 1</code></pre>
<p translate="no">Do not translate me</p>
</body>
</html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">test-book</dc:identifier>
    <dc:title>Test Book</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="img" href="Images/cover.png" media-type="image/png"/>
    <item id="css" href="Styles/style.css" media-type="text/css"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
  </spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Nav</title></head>
<body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">
<ol><li><a href="ch1.xhtml">Chapter One</a></li></ol>
</nav></body></html>
"""

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def make_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/Text/ch1.xhtml", CHAPTER)
        z.writestr("OEBPS/Text/nav.xhtml", NAV)
        z.writestr("OEBPS/Images/cover.png", PNG_BYTES)
        z.writestr("OEBPS/Styles/style.css", "p { margin: 0; }")
    return path


def parse(xml: str):
    return etree.fromstring(xml.encode("utf-8"))


def test_leaf_blocks_skip_code_and_notranslate():
    root = parse(CHAPTER.split("?>", 1)[1].split("\n", 1)[1])
    texts = [" ".join(b.itertext()).strip() for b in find_leaf_blocks(root)]
    assert "Chapter One" in texts
    assert any(t.startswith("Plain paragraph") for t in texts)
    assert "x = 1" not in " ".join(texts)
    assert "Do not translate me" not in " ".join(texts)


def test_marker_roundtrip_preserves_tags_and_attributes():
    root = parse('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                 '<p>Hello <em>big</em> and <a href="x.html#a">link</a> <img src="c.png" alt="c"/> end.</p>'
                 '</body></html>')
    p = root.find(f".//{{{XHTML_NS}}}p")
    mb = serialize_block(p)
    assert mb.marked.count("<g") == 3  # em, a, img
    apply_translation(p, mb.originals, mb.marked.replace("Hello", "안녕"))
    assert "안녕" in "".join(p.itertext())
    a = p.find(f"{{{XHTML_NS}}}a")
    assert a.get("href") == "x.html#a"
    img = p.find(f"{{{XHTML_NS}}}img")
    assert img.get("src") == "c.png" and img.get("alt") == "c"
    assert p.find(f"{{{XHTML_NS}}}em").text == "big"


def test_invalid_markup_is_rejected_and_block_untouched():
    root = parse('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                 '<p>A <em>b</em> c</p></body></html>')
    p = root.find(f".//{{{XHTML_NS}}}p")
    mb = serialize_block(p)
    before = etree.tostring(p)
    with pytest.raises(MarkupError):
        rebuild_block(mb.originals, "A <g0>b c")  # 닫는 마커 없음
    with pytest.raises(MarkupError):
        rebuild_block(mb.originals, "A b c")  # 마커 누락
    assert etree.tostring(p) == before


def test_full_epub_translation(tmp_path):
    src = make_epub(tmp_path / "book.epub")
    dst = tmp_path / "book.ko.epub"
    cache = TranslationCache(tmp_path / "cache.sqlite")
    provider = FakeProvider()

    report = translate_epub(src, dst, provider, target_lang="ko", cache=cache, model_id="fake")

    assert report.failed == []
    assert report.translated == report.total > 0
    assert provider.calls >= 1

    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
        # mimetype이 첫 항목이고 무압축이어야 한다
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        # 이미지, CSS는 바이트 단위로 같아야 한다
        assert z.read("OEBPS/Images/cover.png") == PNG_BYTES
        assert z.read("OEBPS/Styles/style.css") == b"p { margin: 0; }"
        chapter = etree.fromstring(z.read("OEBPS/Text/ch1.xhtml"))
        opf = etree.fromstring(z.read("OEBPS/content.opf"))
        nav = etree.fromstring(z.read("OEBPS/Text/nav.xhtml"))

    h1 = chapter.find(f".//{{{XHTML_NS}}}h1")
    assert h1.text == "[KO] Chapter One"
    # 인라인 태그와 링크 대상이 보존된다
    link = chapter.find(f".//{{{XHTML_NS}}}a")
    assert link.get("href") == "ch2.xhtml#s2"
    assert "[KO]" in "".join(link.getparent().itertext())
    # 코드, translate="no" 블록은 그대로다
    assert chapter.find(f".//{{{XHTML_NS}}}code").text == "x = 1"
    assert chapter.find(f".//{{{XHTML_NS}}}p[@translate='no']").text == "Do not translate me"
    # 이미지 태그와 주변 텍스트
    img = chapter.find(f".//{{{XHTML_NS}}}img")
    assert img is not None and img.get("src") == "../Images/cover.png"
    # 언어 메타데이터와 목차
    assert opf.find(f".//{{{'http://purl.org/dc/elements/1.1/'}}}language").text == "ko"
    assert "[KO]" in "".join(nav.itertext())

    # 대응표
    aligned = [a for a in report.alignments if a["status"] == "translated"]
    assert any(a["original"] == "Chapter One" and a["translated"] == "[KO] Chapter One" for a in aligned)


def test_failed_blocks_keep_original_text(tmp_path):
    src = make_epub(tmp_path / "book.epub")
    dst = tmp_path / "out.epub"
    provider = FakeProvider(mode="drop_close")  # 모든 응답의 닫는 마커를 지운다
    report = translate_epub(src, dst, provider, target_lang="ko", cache=None, model_id="fake", )
    # 마커가 없는 블록(h1 등)은 검증을 통과하므로, 실패 목록은 마커가 있는 블록만 포함해야 한다
    with zipfile.ZipFile(dst) as z:
        chapter = etree.fromstring(z.read("OEBPS/Text/ch1.xhtml"))
    p_em = chapter.find(f".//{{{XHTML_NS}}}em")
    assert p_em is not None and p_em.text == "emphasis"  # 원문 태그 유지
    assert report.failed, "마커 짝이 깨진 블록은 실패로 보고돼야 한다"
    assert all(a["status"] == "failed" for a in report.alignments if a["original"].startswith("Plain paragraph"))


def test_cache_avoids_second_provider_call(tmp_path):
    src = make_epub(tmp_path / "book.epub")
    cache = TranslationCache(tmp_path / "cache.sqlite")
    first = FakeProvider()
    translate_epub(src, tmp_path / "a.epub", first, cache=cache, model_id="fake")
    second = FakeProvider()
    report = translate_epub(src, tmp_path / "b.epub", second, cache=cache, model_id="fake")
    assert second.calls == 0
    assert report.cached == report.total


class FlakyProvider:
    """처음 N번은 예외를 던지고, 그 뒤부터 정상 번역한다(429 같은 일시 오류 흉내)."""

    name = "flaky"

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def translate(self, items, target_lang):
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("429 rate limited")
        return {k: "[KO] " + v for k, v in items.items()}


class AlwaysFailProvider:
    name = "dead"

    def translate(self, items, target_lang):
        raise RuntimeError("quota exhausted")


def test_progress_reports_to_total(tmp_path):
    src = make_epub(tmp_path / "book.epub")
    progress = []
    report = translate_epub(
        src, tmp_path / "out.epub", FakeProvider(), model_id="fake",
        on_progress=lambda d, t: progress.append((d, t)),
    )
    assert progress[0] == (0, report.total)
    assert progress[-1] == (report.total, report.total)


def test_transient_errors_are_retried(tmp_path):
    src = make_epub(tmp_path / "book.epub")
    provider = FlakyProvider(failures=2)
    report = translate_epub(
        src, tmp_path / "out.epub", provider, model_id="flaky",
        sleep=lambda s: None,
    )
    assert report.failed == []
    assert provider.calls >= 3


def test_final_failure_is_reported_and_original_kept(tmp_path):
    src = make_epub(tmp_path / "book.epub")
    errors = []
    report = translate_epub(
        src, tmp_path / "out.epub", AlwaysFailProvider(), model_id="dead",
        on_error=errors.append, sleep=lambda s: None,
    )
    assert report.translated == 0
    assert errors and "quota exhausted" in errors[0]
