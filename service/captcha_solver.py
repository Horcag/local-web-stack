"""Opt-in CAPTCHA image completion through one explicitly configured model gateway."""

from __future__ import annotations

import base64
import math
import os
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_MODEL = "gemini-3.8-flash-high"
MAX_IMAGE_BYTES = 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CAPTCHA_PROMPT = (
    "Внимательно по буквам прочитай текст на этой русской капче (слова или цифры). "
    "Напиши только распознанный текст строчными буквами, разделяя слова пробелами; "
    "цифры одного кода пиши подряд. Без кавычек, точки в конце и пояснений."
)
_ERROR_REASONS = frozenset(
    {
        "unconfigured",
        "gateway_timeout",
        "gateway_http_error",
        "gateway_invalid_response",
        "invalid_image",
        "invalid_prompt",
        "invalid_answer",
    }
)


class CaptchaSolverError(Exception):
    """A public error containing no gateway response, credentials, or image."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in _ERROR_REASONS else "gateway_invalid_response"
        super().__init__(self.reason)


def _timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return 15.0
    return min(value, 30.0)


def _normalize_answer(raw: str) -> str | None:
    if not raw.strip():
        return None
    if any(unicodedata.category(char).startswith("C") for char in raw):
        raise CaptchaSolverError("invalid_answer")
    cleaned = raw.strip().strip("\"'«»").strip()
    for prefix in ("текст:", "ответ:", "капча:", "результат:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    cleaned = cleaned.strip("\"'«»").strip()
    if not cleaned:
        return None
    cleaned = " ".join(cleaned.split())
    if cleaned.replace(" ", "").isdigit():
        cleaned = cleaned.replace(" ", "")
    if len(cleaned) > 100 or not all(char.isalnum() or char == " " for char in cleaned):
        raise CaptchaSolverError("invalid_answer")
    if re.search(
        r"\b(распознан\w*|пояснен\w*|изображени\w*|captcha|recognized|explanation)\b"
        r"|\b(the text|the answer|код капчи|вот ответ|это текст)\b",
        cleaned,
        re.IGNORECASE,
    ):
        raise CaptchaSolverError("invalid_answer")
    return cleaned


class CaptchaSolver:
    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_file: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.enabled = enabled
        self._base_url = (base_url or "").strip().rstrip("/")
        self._api_key = api_key.strip() if api_key else None
        self._model = model.strip()
        self._timeout_seconds = _timeout(timeout_seconds)
        self._key_file_valid = True
        if enabled and not self._api_key and api_key_file:
            try:
                self._api_key = Path(api_key_file).read_text(encoding="utf-8").strip() or None
                self._key_file_valid = bool(self._api_key)
            except (OSError, UnicodeError):
                self._key_file_valid = False

    @property
    def configured(self) -> bool:
        try:
            url = urlsplit(self._base_url)
            url.port  # Validate malformed port values before creating a request.
            httpx.URL(self._base_url)
            valid_url = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
            )
        except (ValueError, httpx.InvalidURL):
            return False
        valid_key = not self._api_key or (
            self._api_key.isascii()
            and not any(unicodedata.category(char).startswith("C") for char in self._api_key)
        )
        return bool(valid_url and self._model and self._key_file_valid and valid_key)

    @classmethod
    def from_env(cls) -> CaptchaSolver:
        try:
            timeout = float(os.getenv("LOCAL_WEB_CAPTCHA_TIMEOUT_SECONDS", "15"))
        except ValueError:
            timeout = 15.0
        return cls(
            enabled=os.getenv("LOCAL_WEB_CAPTCHA_ENABLED", "false").strip().lower()
            in {"true", "1", "yes", "on"},
            base_url=os.getenv("LOCAL_WEB_CAPTCHA_BASE_URL"),
            api_key=os.getenv("LOCAL_WEB_CAPTCHA_API_KEY"),
            api_key_file=os.getenv("LOCAL_WEB_CAPTCHA_API_KEY_FILE"),
            model=os.getenv("LOCAL_WEB_CAPTCHA_MODEL", DEFAULT_MODEL),
            timeout_seconds=timeout,
        )

    async def solve(self, image_bytes: bytes) -> str | None:
        content = await self.complete_image(image_bytes, CAPTCHA_PROMPT)
        return _normalize_answer(content) if content is not None else None

    async def complete_image(self, image_bytes: bytes, prompt: str) -> str | None:
        """Return bounded raw model text for validation by a challenge-specific parser."""
        if not self.enabled or not image_bytes:
            return None
        return await self.complete_images([image_bytes], prompt)

    async def complete_images(self, images: Sequence[bytes], prompt: str) -> str | None:
        """Complete one CAPTCHA image, or its image plus a separate order reference."""
        if not self.enabled:
            return None
        if not self.configured:
            raise CaptchaSolverError("unconfigured")
        if not 1 <= len(images) <= 2 or any(
            len(image) > MAX_IMAGE_BYTES or not image.startswith(PNG_SIGNATURE) for image in images
        ):
            raise CaptchaSolverError("invalid_image")
        if not prompt.strip() or len(prompt) > 2000:
            raise CaptchaSolverError("invalid_prompt")
        message_content: list[dict] = [{"type": "text", "text": prompt}]
        for image in images:
            encoded = base64.b64encode(image).decode("ascii")
            message_content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
            )
        body = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": message_content,
                }
            ],
            # Matching several symbols consumes reasoning tokens before the JSON.
            "max_tokens": 4096 if len(images) == 2 else 1000,
            "temperature": 0.0,
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, trust_env=False) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers
                )
                response.raise_for_status()
        except httpx.TimeoutException:
            raise CaptchaSolverError("gateway_timeout") from None
        except httpx.HTTPError:
            raise CaptchaSolverError("gateway_http_error") from None
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, TypeError, KeyError, IndexError):
            raise CaptchaSolverError("gateway_invalid_response") from None
        if not isinstance(content, str) or len(content) > 4000:
            raise CaptchaSolverError("gateway_invalid_response")
        return content
