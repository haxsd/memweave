import contextlib
import http.client
import io
import json
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from memweave.services import Service
from memweave.web import create_http_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.service = Service(self.home)
        self.service.initialize()
        self.server = create_http_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        request_headers = {"Authorization": f"Bearer {self.server.token}"} if authenticated else {}
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload

    def post(self, action, parameters):
        status, headers, payload = self.request(
            "POST", f"/api/{action}", json.dumps(parameters).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        return status, headers, json.loads(payload)

    def test_html_is_served_with_local_only_browser_policy(self):
        status, headers, body = self.request("GET", "/?token=secret", authenticated=False)
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("MemWeave", html)
        self.assertIn("当前任务范围", html)
        self.assertIn("用户范围 · 跨项目", html)
        self.assertIn("Markdown 存放在 %USERPROFILE%", html)
        self.assertIn("全局记忆状态", html)
        self.assertIn("架构已核对", html)
        self.assertIn("当前项目记忆状态", html)
        self.assertIn("纠正架构", html)
        self.assertIn("遗忘架构", html)
        self.assertIn("重新扫描并同步当前来源哈希", html)
        self.assertIn("当前任务接续摘要", html)
        self.assertIn("memweave-project-id", html)
        self.assertIn('id="workspace-v2"', html)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        policy = headers["Content-Security-Policy"]
        for directive in ("connect-src 'self'", "img-src 'self' data:", "object-src 'none'",
                          "frame-ancestors 'none'", "default-src 'none'"):
            self.assertIn(directive, policy)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_status_requires_bearer_header_and_returns_service_envelope(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": self.server.token}):
            status, _, body = self.request("GET", "/api/status", headers=headers, authenticated=False)
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["status"], "error")
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        envelope = json.loads(body)
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(envelope["data"]["phase"], "local_mvp")
        self.assertEqual(envelope["data"]["counts"], {})

    def test_query_token_does_not_authenticate_api(self):
        status, _, _ = self.request(
            "GET", f"/api/status?token={self.server.token}", authenticated=False,
        )
        self.assertEqual(status, 401)

    def test_servers_bind_loopback_and_generate_independent_secrets(self):
        other = create_http_server(self.service, port=0)
        self.addCleanup(other.server_close)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        parsed = urlsplit(self.server.url)
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(parsed.port, self.server.server_port)
        self.assertEqual(parse_qs(parsed.query)["token"], [self.server.token])
        self.assertGreaterEqual(len(self.server.token), 32)
        self.assertNotEqual(self.server.token, other.token)

    def test_server_url_can_carry_a_project_scope_hint(self):
        root = Path(self.temporary.name) / "project scope"
        root.mkdir()
        server = create_http_server(self.service, port=0, preferred_root=root)
        self.addCleanup(server.server_close)
        query = parse_qs(urlsplit(server.url).query)
        self.assertEqual(query["project"], [str(root.resolve())])
        self.assertEqual(query["token"], [server.token])

    def test_host_and_origin_are_checked_before_serving_html_or_api(self):
        port = self.server.server_port
        for host in ("attacker.example", f"127.0.0.1:{port + 1}", "127.0.0.1",
                     f"localhost:{port}.attacker.example"):
            for path in ("/", "/api/status"):
                with self.subTest(host=host, path=path):
                    status, _, _ = self.request("GET", path, headers={"Host": host})
                    self.assertEqual(status, 403)
        for origin in ("https://attacker.example", "null", f"http://localhost:{port}",
                       f"https://127.0.0.1:{port}", f"http://127.0.0.1:{port}/"):
            with self.subTest(origin=origin):
                status, _, _ = self.request("GET", "/api/status", headers={"Origin": origin})
                self.assertEqual(status, 403)
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            status, _, _ = self.request(
                "GET", "/api/status", headers={"Host": host, "Origin": f"http://{host}"},
            )
            self.assertEqual(status, 200)

    def test_cross_site_fetch_is_rejected_even_with_valid_token(self):
        status, _, _ = self.request(
            "POST", "/api/rebuild", b"{}", {"Sec-Fetch-Site": "cross-site",
                                           "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)

    def test_get_cannot_mutate_and_unknown_actions_are_diagnostic_errors(self):
        status, _, _ = self.request("GET", "/api/rebuild")
        self.assertEqual(status, 405)
        status, _, body = self.post("not_an_action", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "RECORD_INVALID")
        status, _, _ = self.request("GET", "/does-not-exist")
        self.assertEqual(status, 404)

    def test_real_mutation_persists_across_service_instances(self):
        status, _, body = self.post("memory_write", {
            "scope": "user", "title": "保留中文", "body": "跨会话读取",
            "sources": [{"type": "user", "reference": "本地工作台输入"}],
            "operation_id": "web-memory",
        })
        self.assertEqual(status, 200)
        restored = Service(self.home).dispatch("memory_read", {"memory_id": body["data"]["id"]})
        self.assertEqual(restored["data"]["body"], "跨会话读取")
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(json.loads(body)["data"]["counts"]["memory"], 1)

    def test_status_post_validates_parameters_like_other_service_actions(self):
        status, _, body = self.post("status", {"unexpected": "parameter"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "RECORD_INVALID")

    def test_project_status_and_architecture_lifecycle_use_web_actions(self):
        root = Path(self.temporary.name) / "project"
        root.mkdir()
        project = self.post("project_register", {
            "root": str(root), "name": "web graph", "operation_id": "web-project",
        })
        self.assertEqual(project[0], 200)
        project_id = project[2]["data"]["id"]
        source_file = root / "main.py"
        source_file.write_text("print('v1')\n", encoding="utf-8")
        scan = self.post("project_scan", {"project_id": project_id, "operation_id": "web-scan"})
        self.assertEqual(scan[0], 200)
        source = self.post("evidence_read", {"project_id": project_id, "path": "main.py"})
        self.assertEqual(source[0], 200)
        source_hash = source[2]["data"]["hash"]
        written = self.post("graph_write", {
            "project_id": project_id, "operation_id": "web-architecture",
            "title": "入口架构", "body": "main.py 是入口",
            "sources": [{"path": "main.py", "hash": source_hash}],
            "relations": [{
                "source": "self", "target": "file:main.py", "relation": "describes",
                "evidence_kind": "host_reported", "certainty": "confirmed",
            }],
        })
        self.assertEqual(written[0], 200)
        architecture = written[2]["data"]
        status, _, body = self.post("status", {"project_id": project_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["project"]["architecture"]["total"], 1)
        self.assertEqual(body["data"]["project"]["architecture"]["stale"], 0)

        source_file.write_text("print('v2')\n", encoding="utf-8")
        status, _, body = self.post("status", {"project_id": project_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["project"]["architecture"]["stale"], 1)
        queried = self.post("graph_query", {
            "project_id": project_id, "query": "入口架构", "limit": 100,
            "include_stale": True,
        })
        stale_node = next(
            item for item in queried[2]["data"]["nodes"]
            if item["id"] == architecture["id"]
        )
        self.assertEqual(stale_node["revision"], 1)
        self.assertEqual(stale_node["etag"], architecture["etag"])
        self.assertEqual(stale_node["stale_reasons"], ["source_hash_changed"])
        self.assertEqual(stale_node["relations"], [{
            "source": "self", "target": "file:main.py", "relation": "describes",
            "evidence_kind": "host_reported", "certainty": "confirmed",
        }])

        rejected = self.post("graph_update", {
            "project_id": project_id, "operation_id": "web-architecture-stale-update",
            "architecture_id": architecture["id"], "expected_revision": 1,
            "expected_etag": architecture["etag"], "title": "入口架构 v2",
            "body": "main.py 仍是入口",
            "sources": [{"path": "main.py", "hash": source_hash}], "relations": [],
        })
        self.assertEqual(rejected[0], 400)
        self.assertEqual(rejected[2]["error"]["code"], "SOURCE_CHANGED")

        self.post("project_scan", {"project_id": project_id, "operation_id": "web-rescan"})
        refreshed = self.post("evidence_read", {"project_id": project_id, "path": "main.py"})
        updated = self.post("graph_update", {
            "project_id": project_id, "operation_id": "web-architecture-update",
            "architecture_id": architecture["id"], "expected_revision": 1,
            "expected_etag": architecture["etag"], "title": "入口架构 v2",
            "body": "main.py 仍是入口",
            "sources": [{"path": "main.py", "hash": refreshed[2]["data"]["hash"]}],
            "relations": stale_node["relations"],
        })
        self.assertEqual(updated[0], 200)
        updated_node = updated[2]["data"]
        self.assertEqual(updated_node["revision"], 2)
        status, _, body = self.post("status", {"project_id": project_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["project"]["architecture"]["stale"], 0)

        forgotten = self.post("graph_forget", {
            "project_id": project_id, "operation_id": "web-architecture-forget",
            "architecture_id": architecture["id"], "expected_revision": 2,
            "expected_etag": updated_node["etag"],
        })
        self.assertEqual(forgotten[0], 200)
        self.assertTrue(forgotten[2]["data"]["deleted"])
        status, _, body = self.post("status", {"project_id": project_id})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["project"]["architecture"]["total"], 0)
        queried = self.post("graph_query", {
            "project_id": project_id, "query": "", "limit": 100,
            "include_stale": True,
        })
        self.assertFalse(any(
            item["type"] == "architecture" for item in queried[2]["data"]["nodes"]
        ))

    def test_maximum_json_body_is_accepted(self):
        body = b"{}" + b" " * (262144 - 2)
        status, _, payload = self.request(
            "POST", "/api/status", body, {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"]["counts"], {})

    def test_invalid_json_and_non_object_bodies_are_rejected_and_closed(self):
        for body in (b"{", b"[]", b"null", b"42", b'{"bad":"\xff"}', b'{"x":NaN}'):
            with self.subTest(body=body):
                status, headers, payload = self.request(
                    "POST", "/api/status", body, {"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(headers["Connection"], "close")
                self.assertEqual(json.loads(payload)["error"]["code"], "RECORD_INVALID")

    def test_body_limits_and_transfer_encodings_fail_without_reading_body(self):
        cases = [
            ({"Content-Length": "262145"}, 413),
            ({"Content-Length": "9" * 5000}, 413),
            ({"Content-Length": "-1"}, 400),
            ({"Content-Length": "no"}, 400),
            ({"Transfer-Encoding": "chunked"}, 400),
            ({"Content-Encoding": "gzip"}, 415),
            ({"Content-Type": "text/plain"}, 415),
        ]
        for headers, expected_status in cases:
            with self.subTest(headers={name: value[:40] for name, value in headers.items()}):
                status, response_headers, _ = self.request(
                    "POST", "/api/status", b"{}", {"Content-Type": "application/json", **headers},
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(response_headers["Connection"], "close")

    def test_malformed_absolute_request_target_returns_json_error(self):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(
                f"GET http://[ HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n\r\n".encode("ascii")
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 400)
            self.assertEqual(response.getheader("Connection"), "close")
            self.assertEqual(json.loads(response.read())["status"], "error")

    def test_unauthenticated_post_is_closed_without_executing(self):
        status, headers, _ = self.request(
            "POST", "/api/memory_write", b"{}", {"Content-Type": "application/json"},
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(self.service.status()["counts"], {})

    def test_duplicate_host_and_length_headers_are_rejected(self):
        port = self.server.server_port
        for extra, expected_status in (
            (f"Host: 127.0.0.1:{port}\r\n", 403),
            ("Content-Length: 2\r\nContent-Length: 2\r\n", 400),
        ):
            with self.subTest(extra=extra):
                with socket.create_connection(self.server.server_address, timeout=3) as connection:
                    message = (
                        f"POST /api/status HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                        f"Authorization: Bearer {self.server.token}\r\n"
                        f"Content-Type: application/json\r\n{extra}\r\n{{}}"
                    ).encode("ascii")
                    connection.sendall(message)
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, expected_status)
                    self.assertEqual(response.getheader("Connection"), "close")
                    response.read()

    def test_requests_do_not_log_secrets_queries_or_bodies(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.request("GET", f"/?token={self.server.token}", authenticated=False)
            self.request("POST", "/api/status?secret=query", b'{"secret":"body"}',
                         {"Content-Type": "application/json"})
        self.assertEqual(output.getvalue(), "")

    def test_serve_ui_prints_url_and_exits_cleanly_on_keyboard_interrupt(self):
        script = (
            "import _thread, sys, threading\n"
            "from pathlib import Path\n"
            "from memweave.services import Service\n"
            "from memweave.web import serve_ui\n"
            "def interrupt():\n"
            "    sys.stdin.readline()\n"
            "    _thread.interrupt_main()\n"
            "threading.Thread(target=interrupt, daemon=True).start()\n"
            "serve_ui(Service(Path(sys.argv[1])), port=0, open_browser=False)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.home)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stderr.close)
        self.addCleanup(child.stdin.close)
        def stop_child():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        self.addCleanup(stop_child)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(child.stdout.readline()), daemon=True)
        reader.start()
        printed = lines.get(timeout=5).strip()
        parsed = urlsplit(printed)
        self.assertEqual(parsed.hostname, "127.0.0.1")
        token = parse_qs(parsed.query)["token"][0]
        with contextlib.closing(http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)) as connection:
            connection.request("GET", "/api/status", headers={"Authorization": f"Bearer {token}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["status"], "ok")
        child.stdin.write("\n")
        child.stdin.flush()
        _, errors = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, errors)
        self.assertEqual(errors, "")


if __name__ == "__main__":
    unittest.main()
