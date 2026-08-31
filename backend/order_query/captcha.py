"""Captcha signing and local OCR support."""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Callable

from .errors import CaptchaRecognizerUnavailable

CAPTCHA_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{4}")


def captcha_sign(code: str, ip: str) -> str:
    inner = hashlib.md5(f"{code}{ip}".encode("utf-8")).hexdigest()
    return hashlib.md5(f"{inner}JING".encode("utf-8")).hexdigest()


def normalize_captcha_code(value: Any) -> str:
    code = re.sub(r"\s+", "", str(value or ""))
    return code if CAPTCHA_CODE_PATTERN.fullmatch(code) else ""


class CaptchaRecognizer:
    """Load the OCR model on first use and serialize model inference."""

    def __init__(self, classifier_factory: Callable[[], Any] | None = None) -> None:
        self._classifier_factory = classifier_factory
        self._classifier: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._classifier is not None:
            return self._classifier
        try:
            if self._classifier_factory is not None:
                classifier = self._classifier_factory()
            else:
                import ddddocr

                # The shop captcha mixes the four dark glyphs with noisy
                # pastel characters.  ddddocr's current model often returns
                # a plausible but incorrect four-character value, while the
                # legacy model remains tuned for this image format.
                try:
                    classifier = ddddocr.DdddOcr(show_ad=False, old=True)
                except TypeError:
                    # Keep compatibility with older ddddocr releases that do
                    # not expose the ``old`` model switch.
                    classifier = ddddocr.DdddOcr(show_ad=False)
        except Exception as exc:
            raise CaptchaRecognizerUnavailable("本地验证码识别组件未就绪") from exc
        self._classifier = classifier
        return classifier

    def recognize(self, image: bytes) -> str:
        if not image:
            return ""
        with self._lock:
            try:
                value = self._load().classification(image)
            except CaptchaRecognizerUnavailable:
                raise
            except Exception:
                return ""
        return normalize_captcha_code(value)
