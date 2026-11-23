"""WebView User-Agent must cross the Playwright browser boundary."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from playwright.async_api import async_playwright

from hh_applicant_tool.operations.authorize import Operation


def test_configured_webview_user_agent_reaches_browser_request():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers["User-Agent"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    operation = Operation.__new__(Operation)
                    operation._tool = type("Tool", (), {"config": {"webview_user_agent": "Custom WebView"}})()
                    options = operation.browser_context_options(pw.devices["Galaxy A55"])
                    context = await browser.new_context(**options)
                    page = await context.new_page()
                    await page.goto(f"http://127.0.0.1:{server.server_port}/")
                    await context.close()
                finally:
                    await browser.close()

        asyncio.run(check())
        assert received == ["Custom WebView"]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
