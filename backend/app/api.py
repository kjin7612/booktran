"""Book_Tran 백엔드 API (개인용 MVP).

엔드포인트
- POST /documents                      EPUB 업로드
- POST /documents/{id}/translate       번역 작업 시작 (백그라운드)
- GET  /jobs/{job_id}                  작업 상태와 결과 요약
- GET  /documents/{id}/original        원본 EPUB
- GET  /documents/{id}/translated      번역 EPUB
- GET  /documents/{id}/alignments      원문/번역 문단 대응표 (나란히 보기용)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from threading import Lock

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .epub_translate import TranslationCache, translate_epub
from .pdf_translate import translate_pdf
from .providers.gemini import GeminiProvider

DATA_DIR = Path(os.environ.get("BOOK_TRAN_DATA", Path(__file__).resolve().parents[1] / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
for d in (UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

_cache = TranslationCache(DATA_DIR / "cache.sqlite")
_jobs: dict[str, dict] = {}
_jobs_lock = Lock()

app = FastAPI(title="Book_Tran API", version="0.1.0")


# 번역 도착 언어. 파일 이름과 캐시 키에 들어가므로 목록에 있는 코드만 받는다.
SUPPORTED_LANGS = {"ko": "한국어", "en": "영어", "ja": "일본어", "zh": "중국어", "fr": "프랑스어", "de": "독일어", "es": "스페인어"}
# 번역 PDF는 맑은 고딕(한국어 글꼴) 하나로 글자를 쓰므로, 그 글꼴이 덮는 언어만 허용한다.
PDF_LANGS = {"ko", "en", "fr", "de", "es"}


def _check_lang(lang: str, suffix: str) -> None:
    if lang not in SUPPORTED_LANGS:
        raise HTTPException(400, f"지원하지 않는 언어입니다: {lang}")
    if suffix == ".pdf" and lang not in PDF_LANGS:
        raise HTTPException(400, "PDF 번역은 한국어·영어·프랑스어·독일어·스페인어만 지원합니다(글꼴 제약). EPUB은 모든 언어를 지원합니다.")


class TranslateRequest(BaseModel):
    target_lang: str = "ko"


def _doc_path(doc_id: str) -> Path:
    if not doc_id.isalnum():
        raise HTTPException(400, "잘못된 문서 ID")
    for ext in (".epub", ".pdf"):
        path = UPLOAD_DIR / f"{doc_id}{ext}"
        if path.exists():
            return path
    raise HTTPException(404, "문서를 찾을 수 없습니다")


def _out_path(doc_id: str, suffix: str) -> Path:
    return OUTPUT_DIR / f"{doc_id}.{suffix}"


def _translated_path(doc_id: str, lang: str) -> Path | None:
    """번역 결과 파일(EPUB 또는 PDF). 없으면 None."""
    for ext in (".epub", ".pdf"):
        path = _out_path(doc_id, f"{lang}{ext}")
        if path.exists():
            return path
    return None


MEDIA_TYPES = {".epub": "application/epub+zip", ".pdf": "application/pdf"}


@app.post("/documents")
async def upload(file: UploadFile):
    if not (file.filename or "").lower().endswith(".epub"):
        raise HTTPException(400, "EPUB 파일만 지원합니다")
    doc_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{doc_id}.epub"
    dest.write_bytes(await file.read())
    return {"id": doc_id, "filename": file.filename}


def _run_translation(job_id: str, doc_id: str, target_lang: str) -> None:
    def set_state(**kwargs):
        with _jobs_lock:
            _jobs[job_id].update(kwargs)

    try:
        provider = GeminiProvider()
        set_state(status="running", engine=provider.name)
        source = _doc_path(doc_id)
        out = _out_path(doc_id, f"{target_lang}{source.suffix}")
        concurrency = int(os.environ.get("GEMINI_CONCURRENCY", "3"))
        progress = lambda done, total: set_state(progress={"done": done, "total": total})  # noqa: E731
        error = lambda msg: set_state(last_error=msg)  # noqa: E731
        if source.suffix == ".pdf":
            report = translate_pdf(
                source, out, provider, target_lang=target_lang, cache=_cache,
                model_id=provider.name, concurrency=concurrency,
                on_progress=progress, on_error=error,
            )
            extra = {"pages": report.pages, "pages_without_text": report.pages_without_text,
                     "overflow": report.overflow}
        else:
            report = translate_epub(
                source, out, provider,
                target_lang=target_lang, cache=_cache, model_id=provider.name,
                concurrency=concurrency, on_progress=progress, on_error=error,
            )
            extra = {}

        if report.total > 0 and report.translated == 0:
            with _jobs_lock:
                reason = _jobs[job_id].get("last_error", "알 수 없는 오류")
            set_state(status="failed", error=f"번역 엔진 호출이 모두 실패했습니다: {reason}")
            return

        _out_path(doc_id, f"{target_lang}.alignments.json").write_text(
            json.dumps(report.alignments, ensure_ascii=False), encoding="utf-8"
        )
        set_state(
            status="done",
            total=report.total,
            translated=report.translated,
            cached=report.cached,
            failed=len(report.failed),
            **extra,
        )
    except Exception as exc:  # 작업 실패는 상태로 보고한다
        set_state(status="failed", error=f"{type(exc).__name__}: {exc}")


@app.post("/documents/{doc_id}/translate", status_code=202)
def start_translation(doc_id: str, body: TranslateRequest, tasks: BackgroundTasks):
    _check_lang(body.target_lang, _doc_path(doc_id).suffix)
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(503, "GEMINI_API_KEY가 설정되지 않았습니다")
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "doc_id": doc_id, "target_lang": body.target_lang}
    tasks.add_task(_run_translation, job_id, doc_id, body.target_lang)
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return job


@app.get("/documents/{doc_id}/original")
def original(doc_id: str):
    path = _doc_path(doc_id)
    return FileResponse(path, media_type=MEDIA_TYPES[path.suffix])


@app.get("/documents/{doc_id}/translated")
def translated(doc_id: str, lang: str = "ko"):
    _check_lang(lang, _doc_path(doc_id).suffix)
    path = _translated_path(doc_id, lang)
    if path is None:
        raise HTTPException(404, "번역 결과가 없습니다")
    return FileResponse(path, media_type=MEDIA_TYPES[path.suffix])


@app.get("/documents/{doc_id}/alignments")
def alignments(doc_id: str, lang: str = "ko"):
    _check_lang(lang, _doc_path(doc_id).suffix)
    path = _out_path(doc_id, f"{lang}.alignments.json")
    if not path.exists():
        raise HTTPException(404, "대응표가 없습니다")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- 로컬 라이브러리 (브라우저 화면용)
#
# 지정한 폴더(기본 D:\downloads) 바로 아래의 파일만 다룬다. 하위 폴더와 경로 이동은 허용하지 않는다.

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
LIBRARY_FORMATS = {".epub", ".pdf"}


SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULT_LIBRARY = "D:/downloads"


def _saved_folder() -> str | None:
    if not SETTINGS_FILE.exists():
        return None
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("library")
    except (ValueError, OSError):
        return None


def _library_root() -> Path:
    """사용자가 화면에서 고른 폴더 > BOOK_TRAN_LIBRARY 환경변수 > 기본값(D:/downloads)."""
    saved = _saved_folder()
    if saved:
        return Path(saved)
    return Path(os.environ.get("BOOK_TRAN_LIBRARY", DEFAULT_LIBRARY))


def _library_file(name: str) -> Path:
    root = _library_root().resolve()
    if not name or Path(name).name != name:
        raise HTTPException(400, "잘못된 파일 이름")
    path = (root / name).resolve()
    if path.parent != root or not path.is_file() or path.suffix.lower() not in LIBRARY_FORMATS:
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return path


def _library_doc_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:32]


class LocalTranslateRequest(BaseModel):
    name: str
    target_lang: str = "ko"


@app.get("/ui", response_class=HTMLResponse)
def web_ui():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8")


class FolderRequest(BaseModel):
    path: str


@app.get("/local/folder")
def get_folder():
    return {"path": str(_library_root())}


@app.put("/local/folder")
def set_folder(body: FolderRequest):
    """라이브러리 폴더를 바꾼다. 로컬 경로(D:\\books)와 네트워크 경로(\\\\NAS\\books) 모두 쓸 수 있다."""
    raw = body.path.strip().strip('"')
    if not raw:
        raise HTTPException(400, "폴더 경로를 입력하세요")
    path = Path(raw)
    if not path.is_dir():
        raise HTTPException(400, f"폴더를 찾을 수 없습니다: {raw}")
    SETTINGS_FILE.write_text(json.dumps({"library": str(path)}, ensure_ascii=False), encoding="utf-8")
    return {"path": str(path)}


@app.get("/local/files")
def local_files():
    root = _library_root()
    if not root.is_dir():
        raise HTTPException(500, f"라이브러리 폴더가 없습니다: {root}")
    items = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() not in LIBRARY_FORMATS:
            continue
        doc_id = _library_doc_id(path.resolve())
        items.append({
            "name": path.name,
            "format": path.suffix.lower()[1:],
            "size": path.stat().st_size,
            "doc_id": doc_id,
            "translated": _translated_path(doc_id, "ko") is not None,
        })
    return items


@app.get("/local/file")
def local_file(name: str):
    path = _library_file(name)
    media = "application/epub+zip" if path.suffix.lower() == ".epub" else "application/pdf"
    return FileResponse(path, media_type=media)


@app.post("/local/translate", status_code=202)
def translate_local(body: LocalTranslateRequest, tasks: BackgroundTasks):
    path = _library_file(body.name)
    _check_lang(body.target_lang, path.suffix.lower())
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(503, "GEMINI_API_KEY가 설정되지 않았습니다")

    doc_id = _library_doc_id(path)
    dest = UPLOAD_DIR / f"{doc_id}{path.suffix.lower()}"
    if not dest.exists():
        shutil.copyfile(path, dest)

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "doc_id": doc_id, "target_lang": body.target_lang}
    tasks.add_task(_run_translation, job_id, doc_id, body.target_lang)
    return {"job_id": job_id, "doc_id": doc_id}
