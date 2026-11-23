"""User-Agent из конфига должен попасть в настоящий HTTP-запрос сайта hh."""

import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from hh_applicant_tool.main import HHApplicantTool


def prepared_site_request(tmp_path, *, configured, cli=None):
    tool = HHApplicantTool.__new__(HHApplicantTool)
    tool.config = {"user_agent": configured}
    tool.user_agent = cli
    tool._get_proxies = lambda: {}
    tool.cookies_file = tmp_path / "cookies.txt"
    tool._apply_site_language = lambda _jar: None
    session = tool.session
    return session.prepare_request(requests.Request("GET", "https://hh.ru/settings"))


def test_site_request_uses_configured_user_agent(tmp_path):
    request = prepared_site_request(tmp_path, configured="Configured Browser")
    assert request.headers["User-Agent"] == "Configured Browser"


def test_cli_user_agent_overrides_config_for_site_request(tmp_path):
    request = prepared_site_request(
        tmp_path, configured="Configured Browser", cli="CLI Browser"
    )
    assert request.headers["User-Agent"] == "CLI Browser"


def test_default_site_user_agent_tracks_upstream(tmp_path):
    request = prepared_site_request(tmp_path, configured=None)
    assert "Chrome/156.0.0.0" in request.headers["User-Agent"]


def test_configured_user_agent_reaches_http_server(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers["User-Agent"])
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tool = HHApplicantTool.__new__(HHApplicantTool)
        tool.config = {"user_agent": "Configured Browser"}
        tool.user_agent = None
        tool._get_proxies = lambda: {}
        tool.cookies_file = tmp_path / "cookies.txt"
        tool._apply_site_language = lambda _jar: None
        response = tool.session.get(
            f"http://127.0.0.1:{server.server_port}/settings", timeout=3
        )
        assert response.status_code == 200
        assert received == ["Configured Browser"]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
