"""테스트용 번역 어댑터. 네트워크 없이 파이프라인을 검증한다."""

from __future__ import annotations


class FakeProvider:
    """모든 블록 앞에 [KO] 를 붙인다. 마커는 그대로 둔다."""

    name = "fake"

    def __init__(self, mode: str = "prefix"):
        # prefix: 정상 번역 흉내 / drop_close: 닫는 마커를 지워 검증 실패를 일으킴
        self.mode = mode
        self.calls = 0

    def translate(self, items: dict[str, str], target_lang: str) -> dict[str, str]:
        self.calls += 1
        out = {}
        for key, marked in items.items():
            if self.mode == "drop_close":
                out[key] = marked.replace("</g0>", "")
            else:
                out[key] = "[KO] " + marked
        return out
