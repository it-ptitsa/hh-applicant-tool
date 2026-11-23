"""Ответ сайта о потере доступа должен давать понятную ошибку."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests

from hh_applicant_tool.main import Error, HHApplicantTool


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_from_site_http_response(status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = requests.get(
            f"http://127.0.0.1:{server.server_port}/settings", timeout=3
        )
        tool = HHApplicantTool.__new__(HHApplicantTool)
        with pytest.raises(Error, match="повторная авторизация"):
            tool.parse_redirect_config(response)
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
