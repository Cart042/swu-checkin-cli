#!/usr/bin/env python3
"""Exercise the installed browser and OCR runtime without touching school services.

This is intentionally a standalone smoke command instead of a unit test.  It
starts a local HTTP server, launches Playwright's default headless Chromium
shell, and runs ddddocr against a generated, non-sensitive captcha fixture.
"""

from __future__ import annotations

import base64
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


# This image was generated locally with Pillow using the text ``1234``.  It
# contains no account, school, or other user data and keeps the smoke test
# independent of fonts installed by a particular Linux image.
_SYNTHETIC_CAPTCHA = base64.b64decode(
    "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"
)
_EXPECTED_CAPTCHA_TEXT = "1234"


class _LocalPageHandler(BaseHTTPRequestHandler):
    """Serve one deterministic page and reject anything else."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/":
            self.send_error(404)
            return
        body = b"<!doctype html><html><head><title>runtime smoke</title></head>" \
            b"<body><main id='ready'>Chromium runtime ready</main></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _check_browser() -> tuple[str, str]:
    """Launch the default headless Chromium shell and read a local page."""

    from playwright.sync_api import sync_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalPageHandler)
    thread = Thread(target=server.serve_forever, name="runtime-smoke-http", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        with sync_playwright() as playwright:
            # Do not specify a channel: this exercises the headless shell
            # installed by ``playwright install --only-shell chromium``.
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--no-proxy-server",
                ],
            )
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=10_000)
                title = page.title()
                text = page.locator("#ready").inner_text()
                if title != "runtime smoke" or text != "Chromium runtime ready":
                    raise RuntimeError(f"unexpected local page content: title={title!r}, text={text!r}")
                return browser.version, url
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _check_ocr() -> str:
    """Initialize ddddocr and classify a synthetic captcha image."""

    import ddddocr

    ocr = ddddocr.DdddOcr(show_ad=False)
    result = ocr.classification(_SYNTHETIC_CAPTCHA)
    if result != _EXPECTED_CAPTCHA_TEXT:
        raise RuntimeError(
            f"ddddocr synthetic classification mismatch: expected={_EXPECTED_CAPTCHA_TEXT!r}, result={result!r}"
        )
    return result


def main() -> int:
    try:
        browser_version, local_url = _check_browser()
        ocr_result = _check_ocr()
    except Exception as exc:
        print(f"runtime smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"Chromium headless shell OK: version={browser_version}, page={local_url}")
    print(f"ddddocr synthetic classification OK: result={ocr_result!r}")
    print("runtime smoke passed (local-only; no school network or account credentials used)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
