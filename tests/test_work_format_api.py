"""Формат работы из CLI доходит до HTTP-запроса поиска вакансий."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import requests

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.operations.apply_vacancies import Operation


def test_work_format_filter_reaches_vacancy_api():
    operation = Operation()
    parser = argparse.ArgumentParser()
    operation.setup_parser(parser)
    args = parser.parse_args(["--work-format", "REMOTE", "HYBRID"])
    operation.__dict__.update(vars(args))
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(parse_qs(urlsplit(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ApiClient(
            base_url=f"http://127.0.0.1:{server.server_port}/",
            session=requests.Session(),
            delay=0.001,
        )
        client.get("/vacancies", operation._get_search_params(page=0))
        assert received[0]["work_format"] == ["REMOTE", "HYBRID"]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_upstream_force_letter_alias_preserves_fork_behavior():
    operation = Operation()
    parser = argparse.ArgumentParser()
    operation.setup_parser(parser)
    assert parser.parse_args(["--force-cover-letter"]).force_message is True
    assert parser.parse_args(["--no-force-cover-letter"]).force_message is False
