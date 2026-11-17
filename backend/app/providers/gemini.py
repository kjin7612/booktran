"""Gemini 번역 어댑터 (기본 엔진)."""

from __future__ import annotations

import json
import os

from google import genai
from google.genai import types

# 2.5 계열은 새 사용자에게 종료되어 404가 난다. 키가 접근 가능한 모델은 GEMINI_MODEL로 바꿀 수 있다.
DEFAULT_MODEL = "gemini-3.5-flash"

PROMPT_TEMPLATE = """You are a professional book translator.
Translate every value in the JSON object below into {target_lang}.

Rules:
- Keep every tag marker exactly as written: <gN>, </gN>, and <gN/> (N is a number). Do not add, remove, rename, or reorder markers.
- Translate only the text between markers. Keep proper nouns that are better left untranslated.
- Keep numbers, URLs, and code unchanged.
- Return a JSON object with the same keys and translated values. No commentary.

Input:
{payload}
"""


class GeminiProvider:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.name = f"gemini:{self.model}"
        self._client = genai.Client(api_key=key)

    def translate(self, items: dict[str, str], target_lang: str) -> dict[str, str]:
        prompt = PROMPT_TEMPLATE.format(
            target_lang=target_lang,
            payload=json.dumps(items, ensure_ascii=False),
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        )
        # 2.5 계열은 기본으로 생각 토큰을 써서 느리다. 번역에는 필요 없으므로 끈다.
        if self.model.startswith("gemini-2.5"):
            config.thinking_config = types.ThinkingConfig(thinking_budget=0)

        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        data = json.loads(response.text)
        if not isinstance(data, dict):
            raise ValueError("Gemini 응답이 JSON 객체가 아닙니다.")
        return data
