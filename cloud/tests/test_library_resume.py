"""Issue #33: large files go up in chunks, and an interrupted upload continues where it stopped."""
import os

from fastapi.testclient import TestClient

import library_support as lib
from library_support import LibraryCase, sha
from neuxmind_cloud.app import create_app
from support import API


class ResumeTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@resume.test")
        self.data = lib.text_bytes(3 * self.chunk_bytes + 4321)      # four chunks, the last one short

    def test_an_interrupted_upload_continues_from_the_chunks_already_received(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.assertEqual((upload["resumed"], upload["received"], len(parts)), (False, [], 4))
        self.ok(self.put_chunk(self.user, upload["id"], 0, parts[0]))
        self.ok(self.put_chunk(self.user, upload["id"], 2, parts[2]))
        # The connection drops. The client comes back and offers the same file with the same labels.
        again = self.start(self.user, "long.txt", self.data, expect=200)
        self.assertTrue(again["resumed"])
        self.assertEqual(again["id"], upload["id"])
        self.assertEqual(again["received"], [0, 2])
        self.assertEqual(again["received_bytes"], len(parts[0]) + len(parts[2]))
        for index in (1, 3):                                         # only what is missing is sent
            self.ok(self.put_chunk(self.user, upload["id"], index, parts[index]))
        material = self.ok(self.complete(self.user, upload["id"]), 201)
        self.assertEqual(material["sha256"], sha(self.data))
        self.assertEqual(self.content(self.user, material["id"]).content, self.data)

    def test_it_continues_after_the_server_restarts(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.ok(self.put_chunk(self.user, upload["id"], 0, parts[0]))
        self.client.close()
        self.db.engine.dispose()
        restarted = create_app(self.settings)
        try:
            with TestClient(restarted) as client:
                status = client.get(f"{API}/library/uploads/{upload['id']}", headers=self.user["headers"])
                self.assertEqual(self.ok(status)["received"], [0])
                for index in (1, 2, 3):
                    self.ok(self.put_chunk(self.user, upload["id"], index, parts[index], client=client))
                done = client.post(f"{API}/library/uploads/{upload['id']}/complete", json={}, headers=self.user["headers"])
                material = self.ok(done, 201)
                fetched = client.get(f"{API}/library/materials/{material['id']}/content", headers=self.user["headers"])
                self.assertEqual(fetched.content, self.data)
        finally:
            restarted.state.ctx.db.engine.dispose()

    def test_chunks_may_arrive_in_any_order_and_more_than_once(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        for index in (3, 1, 1, 0, 2, 0):
            answer = self.ok(self.put_chunk(self.user, upload["id"], index, parts[index]))
            self.assertEqual(answer["index"], index)
        status = self.ok(self.get(f"/library/uploads/{upload['id']}", self.user))
        self.assertEqual((status["received"], status["received_bytes"]), ([0, 1, 2, 3], len(self.data)))
        material = self.ok(self.complete(self.user, upload["id"]), 201)
        self.assertEqual(self.content(self.user, material["id"]).content, self.data)

    def test_completing_with_chunks_missing_is_refused_and_names_them(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.ok(self.put_chunk(self.user, upload["id"], 0, parts[0]))
        self.ok(self.put_chunk(self.user, upload["id"], 2, parts[2]))
        body = self.error(self.complete(self.user, upload["id"]), 409, "chunks_missing")
        self.assertEqual(body["missing"], [1, 3])
        # Still open: the rest can follow.
        for index in (1, 3):
            self.ok(self.put_chunk(self.user, upload["id"], index, parts[index]))
        self.ok(self.complete(self.user, upload["id"]), 201)

    def test_a_chunk_must_have_exactly_the_expected_length(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.error(self.put_chunk(self.user, upload["id"], 0, parts[0][:-1]), 400, "invalid_chunk")
        self.error(self.put_chunk(self.user, upload["id"], 3, parts[3] + b"x"), 400, "invalid_chunk")
        self.error(self.put_chunk(self.user, upload["id"], 1, b""), 400, "invalid_chunk")
        self.error(self.put_chunk(self.user, upload["id"], 4, parts[0]), 400, "invalid_chunk")
        self.error(self.put_chunk(self.user, upload["id"], -1, b"x"), 400, "invalid_chunk")
        # A negative number is not a chunk address at all, so it does not get the chunk-sized body either.
        self.error(self.put_chunk(self.user, upload["id"], -1, parts[0]), 413, "payload_too_large")
        self.assertEqual(self.ok(self.get(f"/library/uploads/{upload['id']}", self.user))["received"], [])

    def test_a_chunk_needs_the_binary_content_type(self):
        upload = self.start(self.user, "long.txt", self.data)
        part = self.parts(upload, self.data)[0]
        response = self.client.put(f"{API}/library/uploads/{upload['id']}/chunks/0", content=part,
                                   headers={**self.user["headers"], "Content-Type": "text/html"})
        self.error(response, 415, "octet_stream_required")

    def test_a_damaged_chunk_is_refused_when_its_digest_comes_with_it(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        damaged = b"X" + parts[1][1:]
        refused = self.put_chunk(self.user, upload["id"], 1, damaged, **{"X-Chunk-SHA256": sha(parts[1])})
        self.error(refused, 400, "chunk_digest_mismatch")
        self.assertEqual(self.ok(self.get(f"/library/uploads/{upload['id']}", self.user))["received"], [])
        self.ok(self.put_chunk(self.user, upload["id"], 1, parts[1], **{"X-Chunk-SHA256": sha(parts[1])}))
        self.error(self.put_chunk(self.user, upload["id"], 2, parts[2], **{"X-Chunk-SHA256": "zz"}), 400)

    def test_content_that_does_not_match_the_declared_sha256_is_refused_at_the_end(self):
        upload = self.start(self.user, "long.txt", self.data)
        other = lib.text_bytes(len(self.data), line="other content {i}\n")[:len(self.data)]
        for index, part in enumerate(self.parts(upload, other)):
            self.ok(self.put_chunk(self.user, upload["id"], index, part))
        self.error(self.complete(self.user, upload["id"]), 409, "digest_mismatch")
        self.error(self.get(f"/library/uploads/{upload['id']}", self.user), 404)
        self.assertEqual(self.ok(self.get("/library/materials", self.user))["total"], 0)
        self.assertEqual(self.stored_files(), [])

    def test_an_abandoned_upload_expires_and_its_bytes_are_removed(self):
        from sqlalchemy import update

        from neuxmind_cloud.library_models import MaterialUpload
        upload = self.start(self.user, "long.txt", self.data)
        self.ok(self.put_chunk(self.user, upload["id"], 0, self.parts(upload, self.data)[0]))
        self.assertEqual(len(self.stored_files()), 1)
        self.assertGreater(upload["expires_at"], 0)
        with self.db.transaction() as s:
            s.execute(update(MaterialUpload).values(expires_at=1.0))
        # Expired: it can no longer be continued, and the next upload that starts clears it away.
        self.error(self.put_chunk(self.user, upload["id"], 1, self.parts(upload, self.data)[1]), 404)
        self.start(self.user, "other.txt", b"hello\n")
        self.error(self.get(f"/library/uploads/{upload['id']}", self.user), 404)
        self.assertEqual(self.stored_files(), [])

    def test_a_cancelled_upload_frees_its_space(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.ok(self.put_chunk(self.user, upload["id"], 0, self.parts(upload, self.data)[0]))
        self.assertTrue(self.ok(self.delete(f"/library/uploads/{upload['id']}", self.user))["cancelled"])
        self.error(self.get(f"/library/uploads/{upload['id']}", self.user), 404)
        self.assertEqual(self.stored_files(), [])
        # The same file can be started again from nothing.
        fresh = self.start(self.user, "long.txt", self.data)
        self.assertEqual((fresh["resumed"], fresh["received"]), (False, []))

    def test_a_finished_upload_takes_no_more_chunks(self):
        upload = self.start(self.user, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        for index, part in enumerate(parts):
            self.ok(self.put_chunk(self.user, upload["id"], index, part))
        material = self.ok(self.complete(self.user, upload["id"]), 201)
        self.error(self.put_chunk(self.user, upload["id"], 0, b"Z" * len(parts[0])), 404)
        self.error(self.complete(self.user, upload["id"]), 404)
        self.assertEqual(self.content(self.user, material["id"]).content, self.data)
        blob = os.path.join(self.library_dir, self.stored_files()[0])
        with open(blob, "rb") as handle:
            self.assertEqual(sha(handle.read()), sha(self.data))
