import time

import pytest
from fastapi.testclient import TestClient

from app import api
from app.providers.fake import FakeProvider
from tests.test_epub_translate import make_epub
from tests.test_pdf_translate import make_pdf


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    make_epub(root / "sample.epub")
    make_pdf(root / "paper.pdf")
    (root / "notes.txt").write_text("ignored")
    monkeypatch.setenv("BOOK_TRAN_LIBRARY", str(root))
    monkeypatch.setattr(api, "SETTINGS_FILE", tmp_path / "settings.json")  # 실제 설정 파일을 건드리지 않는다
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(api, "GeminiProvider", lambda: FakeProvider())
    return root


def test_lists_only_epub_and_pdf_files(library):
    client = TestClient(api.app)
    names = [f["name"] for f in client.get("/local/files").json()]
    assert names == ["paper.pdf", "sample.epub"]


def test_library_folder_can_be_changed(library, tmp_path, monkeypatch):
    monkeypatch.delenv("BOOK_TRAN_LIBRARY")  # 화면에서 고른 폴더가 환경변수보다 우선한다
    other = tmp_path / "other"
    other.mkdir()
    make_epub(other / "only.epub")
    client = TestClient(api.app)

    res = client.put("/local/folder", json={"path": str(other)})
    assert res.status_code == 200
    names = [f["name"] for f in client.get("/local/files").json()]
    assert names == ["only.epub"]
    assert client.get("/local/folder").json()["path"] == str(other)

    bad = client.put("/local/folder", json={"path": str(tmp_path / "missing")})
    assert bad.status_code == 400


def test_target_language_is_checked(library):
    client = TestClient(api.app)
    # 목록에 없는 언어는 거부한다(파일 이름에 들어가는 값이므로)
    assert client.post("/local/translate", json={"name": "sample.epub", "target_lang": "../x"}).status_code == 400
    # PDF는 글꼴 제약 때문에 일본어를 거부한다
    assert client.post("/local/translate", json={"name": "paper.pdf", "target_lang": "ja"}).status_code == 400
    # EPUB은 일본어도 받는다
    started = client.post("/local/translate", json={"name": "sample.epub", "target_lang": "ja"})
    assert started.status_code == 202


def test_serves_library_file_and_refuses_path_escape(library):
    client = TestClient(api.app)
    res = client.get("/local/file", params={"name": "sample.epub"})
    assert res.status_code == 200
    assert res.content.startswith(b"PK")
    assert client.get("/local/file", params={"name": "../secret.epub"}).status_code == 400
    assert client.get("/local/file", params={"name": "notes.txt"}).status_code == 404


def test_translate_library_pdf_end_to_end(library):
    client = TestClient(api.app)
    started = client.post("/local/translate", json={"name": "paper.pdf", "target_lang": "ko"})
    assert started.status_code == 202
    body = started.json()

    job = {}
    for _ in range(100):
        job = client.get(f"/jobs/{body['job_id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    res = client.get(f"/documents/{body['doc_id']}/translated?lang=ko")
    assert res.status_code == 200
    assert res.content.startswith(b"%PDF")


def test_translate_library_epub_end_to_end(library):
    client = TestClient(api.app)
    started = client.post("/local/translate", json={"name": "sample.epub", "target_lang": "ko"})
    assert started.status_code == 202
    body = started.json()

    job = {}
    for _ in range(100):  # 백그라운드 작업이 끝날 때까지 잠깐 기다린다
        job = client.get(f"/jobs/{body['job_id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job

    listing = {f["name"]: f for f in client.get("/local/files").json()}
    assert listing["sample.epub"]["translated"] is True
    assert client.get(f"/documents/{body['doc_id']}/translated?lang=ko").status_code == 200
    assert client.get(f"/documents/{body['doc_id']}/alignments?lang=ko").status_code == 200
