"""EPUB 번역 파이프라인 (MVP).

원칙
- 블록 요소(p, h1~h6, li, td ...)의 텍스트만 번역하고, 나머지 파일(이미지, CSS, 폰트)은 바이트 그대로 복사한다.
- 인라인 태그(em, a, span ...)는 <gN>…</gN> 마커로 바꿔 번역 엔진에 보내고, 응답의 마커가 정확할 때만 DOM에 되돌린다.
- 검증에 실패한 블록은 원문을 그대로 둔다. 파일이 깨지는 것보다 원문 노출이 낫다.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote

from lxml import etree

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "figcaption", "blockquote", "dt", "dd", "caption",
}
SKIP_TAGS = {"script", "style", "code", "pre", "math", "svg"}

PARSER = etree.XMLParser(resolve_entities=False, huge_tree=True)

TOKEN_RE = re.compile(r"<g(\d+)/>|<g(\d+)>|</g(\d+)>")


class MarkupError(ValueError):
    """번역 응답의 인라인 마커가 원문과 맞지 않을 때."""


class TranslationProvider(Protocol):
    name: str

    def translate(self, items: dict[str, str], target_lang: str) -> dict[str, str]:
        """key -> 마커가 포함된 원문을 받아 key -> 마커가 포함된 번역문을 돌려준다."""
        ...


# ---------------------------------------------------------------- 블록 추출

def _localname(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _is_skipped(el) -> bool:
    node = el
    while node is not None:
        if isinstance(node.tag, str) and (
            _localname(node) in SKIP_TAGS or node.get("translate") == "no"
        ):
            return True
        node = node.getparent()
    return False


def plain_text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def find_leaf_blocks(root, is_ncx: bool = False) -> list:
    """번역 단위가 되는 가장 안쪽 블록 요소 목록을 문서 순서대로 반환한다."""
    if is_ncx:
        return [
            el for el in root.iter()
            if isinstance(el.tag, str) and _localname(el) == "text"
            and el.getparent() is not None and _localname(el.getparent()) == "navLabel"
        ]

    blocks = []
    for el in root.iter():
        if not isinstance(el.tag, str) or _localname(el) not in BLOCK_TAGS:
            continue
        if _is_skipped(el):
            continue
        has_block_child = any(
            isinstance(d.tag, str) and _localname(d) in BLOCK_TAGS
            for d in el.iterdescendants()
        )
        if has_block_child:
            continue
        if not plain_text(el):
            continue
        blocks.append(el)
    return blocks


# ---------------------------------------------------------------- 마커 직렬화 / 복원

@dataclass
class MarkedBlock:
    marked: str
    originals: list[tuple[str, dict]]  # 인덱스 N -> (태그, 속성)


def serialize_block(block) -> MarkedBlock:
    originals: list[tuple[str, dict]] = []
    parts: list[str] = []

    def walk(el):
        parts.append(el.text or "")
        for child in el:
            if isinstance(child.tag, str):
                idx = len(originals)
                originals.append((child.tag, dict(child.attrib)))
                if len(child) == 0 and not child.text:
                    parts.append(f"<g{idx}/>")
                else:
                    parts.append(f"<g{idx}>")
                    walk(child)
                    parts.append(f"</g{idx}>")
            parts.append(child.tail or "")

    walk(block)
    return MarkedBlock("".join(parts), originals)


def rebuild_block(originals: list[tuple[str, dict]], translated: str):
    """마커 문자열을 새 요소 트리(임시 루트)로 복원한다. 검증 실패 시 MarkupError."""
    if not translated.strip():
        raise MarkupError("빈 번역문")

    scratch = etree.Element("scratch")
    stack: list[tuple[int | None, etree._Element]] = [(None, scratch)]
    seen: set[int] = set()

    def add_text(text: str):
        if not text:
            return
        parent = stack[-1][1]
        if len(parent):
            last = parent[-1]
            last.tail = (last.tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    def make(idx: int):
        if idx in seen or idx >= len(originals):
            raise MarkupError(f"잘못된 마커 g{idx}")
        seen.add(idx)
        tag, attrib = originals[idx]
        return etree.Element(tag, attrib=attrib)

    pos = 0
    for m in TOKEN_RE.finditer(translated):
        add_text(translated[pos:m.start()])
        pos = m.end()
        if m.group(1) is not None:  # 자체 닫힘
            stack[-1][1].append(make(int(m.group(1))))
        elif m.group(2) is not None:  # 여는 태그
            idx = int(m.group(2))
            el = make(idx)
            stack[-1][1].append(el)
            stack.append((idx, el))
        else:  # 닫는 태그
            idx = int(m.group(3))
            if len(stack) == 1 or stack[-1][0] != idx:
                raise MarkupError("마커 짝이 맞지 않음")
            stack.pop()
    add_text(translated[pos:])

    if len(stack) != 1:
        raise MarkupError("닫히지 않은 마커")
    if seen != set(range(len(originals))):
        raise MarkupError("누락된 마커")
    return scratch


def apply_translation(block, originals: list[tuple[str, dict]], translated: str) -> None:
    scratch = rebuild_block(originals, translated)  # 실패하면 block은 건드리지 않는다
    for child in list(block):
        block.remove(child)
    block.text = scratch.text
    for child in list(scratch):
        block.append(child)


# ---------------------------------------------------------------- 번역 캐시

class TranslationCache:
    """문장(마커 문자열) 단위 번역 캐시. 검증을 통과한 번역만 저장한다."""

    def __init__(self, path: str | Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO cache(key, value) VALUES (?, ?)", (key, value))
            self._conn.commit()


def cache_key(model_id: str, target_lang: str, marked: str) -> str:
    return hashlib.sha256(f"{model_id}\x1f{target_lang}\x1f{marked}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 번역 실행

@dataclass
class Unit:
    key: str
    doc: str
    block: object
    marked: str
    originals: list[tuple[str, dict]]
    original_text: str


def translate_units(
    units: list[Unit],
    provider: TranslationProvider,
    target_lang: str,
    model_id: str,
    cache: TranslationCache | None,
    max_chars: int = 12000,
    max_items: int = 60,
    retries: int = 5,
    concurrency: int = 3,
    on_progress: Callable[[int, int], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    sleep=time.sleep,
) -> tuple[dict[str, str], set[str]]:
    """반환: (key -> 번역 마커 문자열, 캐시에서 온 key 집합).

    묶음 요청은 concurrency만큼 동시에 보낸다. 실패는 지수 백오프로 재시도하고,
    최종 실패는 on_error로 알린다(해당 문단은 원문 유지).
    """
    results: dict[str, str] = {}
    cached: set[str] = set()
    pending: list[Unit] = []

    for u in units:
        hit = cache.get(cache_key(model_id, target_lang, u.marked)) if cache else None
        if hit is not None:
            results[u.key] = hit
            cached.add(u.key)
        else:
            pending.append(u)

    total = len(units)
    done = len(cached)
    if on_progress:
        on_progress(done, total)

    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    size = 0
    for u in pending:
        if current and (size + len(u.marked) > max_chars or len(current) >= max_items):
            batches.append(current)
            current, size = {}, 0
        current[u.key] = u.marked
        size += len(u.marked)
    if current:
        batches.append(current)

    def run_batch(batch: dict[str, str]) -> tuple[dict[str, str], dict[str, str] | None]:
        for attempt in range(retries):
            try:
                return batch, provider.translate(batch, target_lang)
            except Exception as exc:  # 네트워크, 할당량(429), 응답 파싱 오류 모두 재시도
                message = f"{type(exc).__name__}: {exc}"
                if attempt == retries - 1:
                    if on_error:
                        on_error(message)
                else:
                    sleep(min(2 ** (attempt + 1), 60))
        return batch, None

    if batches:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(run_batch, b) for b in batches]
            for future in as_completed(futures):
                batch, out = future.result()
                if out:
                    for key in batch:
                        value = out.get(key)
                        if isinstance(value, str):
                            results[key] = value
                done += len(batch)
                if on_progress:
                    on_progress(done, total)
    return results, cached


@dataclass
class Report:
    total: int
    translated: int
    cached: int
    failed: list[str]
    alignments: list[dict]


def translate_epub(
    src: str | Path,
    dst: str | Path,
    provider: TranslationProvider,
    target_lang: str = "ko",
    cache: TranslationCache | None = None,
    model_id: str | None = None,
    concurrency: int = 3,
    on_progress: Callable[[int, int], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    sleep=time.sleep,
) -> Report:
    model_id = model_id or provider.name

    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        container = etree.fromstring(zin.read("META-INF/container.xml"), PARSER)
        opf_path = container.find(f".//{{{CONTAINER_NS}}}rootfile").get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf_root = etree.fromstring(zin.read(opf_path), PARSER)

        def resolve(href: str) -> str:
            return posixpath.normpath(posixpath.join(opf_dir, unquote(href)))

        manifest = {item.get("id"): item for item in opf_root.iter(f"{{{OPF_NS}}}item")}

        # 읽기 순서(spine) + 목차(nav, NCX)
        doc_hrefs: list[str] = []
        for ref in opf_root.iter(f"{{{OPF_NS}}}itemref"):
            item = manifest.get(ref.get("idref"))
            if item is not None and item.get("href") not in doc_hrefs:
                doc_hrefs.append(item.get("href"))
        for item in manifest.values():
            is_nav = "nav" in (item.get("properties") or "").split()
            is_ncx = item.get("media-type") == "application/x-dtbncx+xml"
            if (is_nav or is_ncx) and item.get("href") not in doc_hrefs:
                doc_hrefs.append(item.get("href"))

        # 1) 블록 추출
        units: list[Unit] = []
        trees: dict[str, etree._ElementTree] = {}
        for href in doc_hrefs:
            zpath = resolve(href)
            if zpath not in names:
                continue
            tree = etree.parse(BytesIO(zin.read(zpath)), PARSER)
            trees[zpath] = tree
            blocks = find_leaf_blocks(tree.getroot(), is_ncx=zpath.endswith(".ncx"))
            for idx, block in enumerate(blocks):
                mb = serialize_block(block)
                units.append(Unit(
                    key=f"{zpath}#{idx}", doc=zpath, block=block, marked=mb.marked,
                    originals=mb.originals, original_text=plain_text(block),
                ))

        # 2) 번역
        results, cached_keys = translate_units(
            units, provider, target_lang, model_id, cache,
            concurrency=concurrency, on_progress=on_progress, on_error=on_error, sleep=sleep,
        )

        # 3) 검증 후 적용 (실패 블록은 원문 유지)
        failed: list[str] = []
        alignments: list[dict] = []
        translated_count = 0
        for u in units:
            translated_text = None
            status = "original"
            value = results.get(u.key)
            if value is not None:
                try:
                    apply_translation(u.block, u.originals, value)
                    status = "translated"
                    translated_text = plain_text(u.block)
                    translated_count += 1
                    if cache is not None and u.key not in cached_keys:
                        cache.put(cache_key(model_id, target_lang, u.marked), value)
                except MarkupError:
                    status = "failed"
                    failed.append(u.key)
            else:
                status = "failed"
                failed.append(u.key)
            alignments.append({
                "doc": u.doc,
                "index": int(u.key.rsplit("#", 1)[1]),
                "original": u.original_text,
                "translated": translated_text,
                "status": status,
            })

        # 4) 메타데이터: 대상 언어
        for lang_el in opf_root.iter(f"{{{DC_NS}}}language"):
            lang_el.text = target_lang

        modified: dict[str, bytes] = {
            opf_path: etree.tostring(opf_root, encoding="utf-8", xml_declaration=True),
        }
        changed_docs = {a["doc"] for a in alignments if a["status"] == "translated"}
        for zpath, tree in trees.items():
            if zpath in changed_docs:
                modified[zpath] = etree.tostring(tree, encoding="utf-8", xml_declaration=True)

        # 5) 출력 EPUB: mimetype을 첫 항목으로 무압축 저장, 나머지는 원본 순서 유지
        with zipfile.ZipFile(dst, "w") as zout:
            mimetype = zin.read("mimetype") if "mimetype" in names else b"application/epub+zip"
            zout.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
            for name in names:
                if name == "mimetype":
                    continue
                data = modified[name] if name in modified else zin.read(name)
                zout.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

    return Report(
        total=len(units),
        translated=translated_count,
        cached=len(cached_keys),
        failed=failed,
        alignments=alignments,
    )
