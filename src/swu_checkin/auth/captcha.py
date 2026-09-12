"""验证码识别：进程内共享的 ddddocr 引擎。"""

from __future__ import annotations

import threading

_ocr_init_lock = threading.Lock()
_ocr_classification_lock = threading.Lock()
_ocr_instance = None


def get_ocr():
    """Return the process-wide lazily initialized OCR engine.

    The ddddocr object owns a relatively large model.  Initializing one per
    worker thread multiplies that cost, so construction is guarded and the
    resulting object is shared.  Classification itself is guarded separately
    because the library does not promise that one object is thread-safe.
    """
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_init_lock:
            if _ocr_instance is None:
                import ddddocr

                _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


def classify_captcha(image_bytes):
    """Classify captcha bytes through the shared OCR engine safely."""
    with _ocr_classification_lock:
        return get_ocr().classification(image_bytes)
