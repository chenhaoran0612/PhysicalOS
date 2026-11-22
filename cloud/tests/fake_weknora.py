"""A stand-in for the WeKnora knowledge service (issue #34 tests): the five endpoints the cloud uses.

It keeps what it was sent (files, metadata, questions) so tests can check
what left the cloud, and lets a test decide how parsing goes and what the
"model" answers. Nothing here is WeKnora's code; only the shape of its API.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "test-" + secrets.token_hex(8)        # made at run time: no key literal in the repository


class FakeWeKnora:
    def __init__(self, kb_id: str = "kb-1"):
        self.kb_id = kb_id
        self.lock = threading.Lock()
        self.knowledge: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.questions: list[dict] = []
        self.deleted: list[str] = []
        self.sessions_open: set[str] = set()
        self.counter = 0
        # Hooks a test may replace.
        self.polls_until_ready = 1                   # GETs that still say "processing"
        self.fail_parse = lambda upload: ""          # non-empty: parsing fails with this message
        self.answer = lambda question, knowledge_ids: "{}"
        self.status_override: int | None = None      # every request answered with this HTTP status
        self.drop_before_chat = False                # the next question: the copy is gone first (deleted elsewhere)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    # ------------------------------------------------------------ handler
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: dict) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length", "0")))

            def _guard(self) -> bool:
                if fake.status_override:
                    self._body()
                    self._send(fake.status_override, {"success": False, "message": "busy"})
                    return False
                if self.headers.get("X-API-Key") != API_KEY:
                    self._body()
                    self._send(401, {"success": False, "message": "unauthorized"})
                    return False
                return True

            def do_POST(self):
                if not self._guard():
                    return
                path = self.path.split("?", 1)[0]
                upload = re.fullmatch(r"/api/v1/knowledge-bases/([^/]+)/knowledge/file", path)
                chat = re.fullmatch(r"/api/v1/knowledge-chat/([^/]+)", path)
                if upload:
                    return self._upload(upload.group(1))
                if path == "/api/v1/sessions":
                    self._body()
                    with fake.lock:
                        session_id = fake._next_id("sess")
                        fake.sessions_open.add(session_id)
                    return self._send(200, {"success": True, "data": {"id": session_id}})
                if chat:
                    return self._chat(chat.group(1))
                self._body()
                self._send(404, {"success": False, "message": "no route"})

            def _upload(self, kb_id: str):
                body = self._body()
                boundary = self.headers.get("Content-Type", "").split("boundary=", 1)[-1].encode()
                parts = {}
                for part in body.split(b"--" + boundary):
                    head, _, content = part.partition(b"\r\n\r\n")
                    name = re.search(rb'name="([^"]+)"', head)
                    if name:
                        filename = re.search(rb'filename="([^"]*)"', head)
                        parts[name.group(1).decode()] = (content[:-2], filename.group(1).decode() if filename else None)
                data, part_name = parts.get("file", (b"", None))
                record = {"kb_id": kb_id, "part_filename": part_name, "fileName": parts.get("fileName", (b"",))[0].decode(),
                          "metadata": json.loads(parts.get("metadata", (b"{}",))[0] or b"{}"), "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()}
                with fake.lock:
                    fake.uploads.append(record)
                    existing = next((k for k in fake.knowledge.values() if k["sha256"] == record["sha256"]), None)
                    if existing is not None:
                        return self._send(409, {"success": False, "data": fake._record(existing)})
                    knowledge = {"id": fake._next_id("kn"), "sha256": record["sha256"], "status": "pending",
                                 "polls": 0, "error": fake.fail_parse(record), "upload": record}
                    fake.knowledge[knowledge["id"]] = knowledge
                self._send(200, {"success": True, "data": fake._record(knowledge)})

            def _chat(self, session_id: str):
                payload = json.loads(self._body() or b"{}")
                with fake.lock:
                    if fake.drop_before_chat:
                        fake.drop_before_chat = False
                        for knowledge_id in payload.get("knowledge_ids", []):
                            fake.knowledge.pop(knowledge_id, None)
                    if any(k not in fake.knowledge for k in payload.get("knowledge_ids", [])):
                        return self._send(404, {"success": False, "message": "knowledge not found"})
                    fake.questions.append({"session": session_id, **payload})
                text = fake.answer(payload.get("query", ""), payload.get("knowledge_ids", []))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                half = len(text) // 2
                events = [{"response_type": "references", "content": "", "knowledge_references": [{"content": "x" * 200}]},
                          {"response_type": "answer", "content": text[:half], "done": False},
                          {"response_type": "answer", "content": text[half:], "done": False},
                          {"response_type": "answer", "content": "", "done": True}]
                for event in events:
                    self.wfile.write(b"event: message\ndata: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
                self.close_connection = True

            def do_GET(self):
                if not self._guard():
                    return
                found = re.fullmatch(r"/api/v1/knowledge/([^/]+)", self.path)
                with fake.lock:
                    knowledge = fake.knowledge.get(found.group(1)) if found else None
                    if knowledge is None:
                        return self._send(404, {"success": False, "message": "not found"})
                    knowledge["polls"] += 1
                    if knowledge["polls"] > fake.polls_until_ready:
                        knowledge["status"] = "failed" if knowledge["error"] else "completed"
                    else:
                        knowledge["status"] = "processing"
                    record = fake._record(knowledge)
                self._send(200, {"success": True, "data": record})

            def do_DELETE(self):
                if not self._guard():
                    return
                knowledge = re.fullmatch(r"/api/v1/knowledge/([^/]+)", self.path)
                session = re.fullmatch(r"/api/v1/sessions/([^/]+)", self.path)
                with fake.lock:
                    if knowledge and knowledge.group(1) in fake.knowledge:
                        del fake.knowledge[knowledge.group(1)]
                        fake.deleted.append(knowledge.group(1))
                        return self._send(200, {"success": True})
                    if session:
                        fake.sessions_open.discard(session.group(1))
                        return self._send(200, {"success": True})
                self._send(404, {"success": False, "message": "not found"})

        return Handler

    @staticmethod
    def _record(knowledge: dict) -> dict:
        return {"id": knowledge["id"], "parse_status": knowledge["status"], "error_message": knowledge["error"],
                "file_name": knowledge["upload"]["fileName"]}
