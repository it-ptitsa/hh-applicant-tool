"""Одноразовый refresh token не должен использоваться двумя потоками."""

import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from urllib.parse import parse_qs

import requests
import pytest

from hh_applicant_tool.api.client import ApiClient, OAuthClient
from hh_applicant_tool.api.errors import BadRequest, Forbidden
from hh_applicant_tool.main import HHApplicantTool
from hh_applicant_tool.utils.config import Config


def test_concurrent_expired_api_requests_refresh_token_once():
    second_old_request = Event()
    guard = Lock()
    seen = {"old_gets": 0, "refreshes": 0, "refresh_tokens": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") == "Bearer USER_NEW":
                status, body = 200, {}
            else:
                with guard:
                    seen["old_gets"] += 1
                    if seen["old_gets"] == 2:
                        second_old_request.set()
                status, body = 403, {"errors": []}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            token = parse_qs(body.decode()).get("refresh_token", [None])[0]
            with guard:
                seen["refreshes"] += 1
                seen["refresh_tokens"].append(token)
                first = seen["refreshes"] == 1
            if first:
                assert second_old_request.wait(timeout=3)
                status = 200
                response = {
                    "access_token": "USER_NEW",
                    "refresh_token": "RT_NEW",
                    "expires_in": 3600,
                }
            else:
                status, response = 400, {"error_description": "token reused"}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/"
        session = requests.Session()
        client = ApiClient(
            base_url=base,
            session=session,
            delay=0.001,
            access_token="USER_OLD",
            refresh_token="RT_OLD",
            access_expires_at=0,
        )
        client.oauth_client = OAuthClient(
            base_url=base + "oauth/",
            session=session,
            delay=0.001,
            client_id="id",
            client_secret="secret",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.get, "/protected") for _ in range(2)]
            assert [future.result(timeout=5) for future in futures] == [{}, {}]
        assert seen == {
            "old_gets": 2,
            "refreshes": 1,
            "refresh_tokens": ["RT_OLD"],
        }
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("refresh_status,error_type", [(400, BadRequest), (403, Forbidden)])
def test_invalid_refresh_token_clears_stale_credentials(tmp_path, refresh_status, error_type):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"errors": []}')

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(refresh_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error_description": "invalid refresh token"}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/"
        session = requests.Session()
        client = ApiClient(
            base_url=base,
            session=session,
            delay=0.001,
            access_token="USER_OLD",
            refresh_token="RT_INVALID",
            access_expires_at=0,
        )
        client.oauth_client = OAuthClient(
            base_url=base + "oauth/", session=session, delay=0.001
        )
        with pytest.raises(error_type):
            client.get("/protected")
        assert client.get_access_token() == {
            "access_token": None,
            "refresh_token": None,
            "access_expires_at": 0,
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"token": {"access_token": "USER_OLD", "refresh_token": "RT_INVALID"}})
        )
        tool = HHApplicantTool.__new__(HHApplicantTool)
        tool.api_client = client
        tool.config = Config(config_path)
        assert tool.save_token()
        assert json.loads(config_path.read_text())["token"] == client.get_access_token()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
