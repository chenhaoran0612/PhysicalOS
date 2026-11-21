"""Issue #33: the paths a well-behaved client never takes — a body sent without a credential, a volume
that is nearly full, an upload stuck in its last step, a file swapped under its name, leftovers on disk."""
import asyncio
import os
import time
from unittest import mock

from sqlalchemy import select, update

import library_support as lib
from library_support import LABELS, LibraryCase, sha
from neuxmind_cloud import app as cloud_app
from neuxmind_cloud import library, maintenance
from neuxmind_cloud.library_models import MaterialUpload
from neuxmind_cloud.library_store import LibraryStore, StagingLost
from support import API

UPLOAD = "upl_" + "0" * 20


class ChunkBodyGuardTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@guard.test")

    def guard(self, path: str, method: str = "PUT", headers: dict | None = None):
        sent = []

        async def send(message):
            sent.append(message)
        raw = [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
        scope = {"type": "http", "method": method, "path": path, "headers": raw, "app": self.app}
        return asyncio.run(library.guard_body(scope, send, 1234)), sent

    def test_the_route_pattern_is_the_real_route(self):
        self.assertTrue(library.CHUNK_ROUTE.fullmatch(f"{cloud_app.API_PREFIX}/library/uploads/{UPLOAD}/chunks/7"))

    def test_only_the_exact_chunk_route_is_treated_differently(self):
        chunk = f"{API}/library/uploads/{UPLOAD}/chunks/0"
        others = [chunk + "/extra", "/evil" + chunk, chunk + "\n", f"{API}/library/uploads/{UPLOAD}/chunks/-1",
                  f"{API}/library/uploads/anything/chunks/0", f"{API}/library/uploads/{UPLOAD.upper()}/chunks/0",
                  f"{API}/library/uploads", f"{API}/robots"]
        for path in others:
            with self.subTest(path=path):
                self.assertEqual(self.guard(path, headers=self.user["headers"]), (1234, []))
        self.assertEqual(self.guard(chunk, "POST", self.user["headers"]), (1234, []))

    def test_a_live_credential_gets_the_chunk_bound_and_nobody_else_gets_past(self):
        chunk = f"{API}/library/uploads/{UPLOAD}/chunks/0"
        self.assertEqual(self.guard(chunk, headers=self.user["headers"]), (self.chunk_bytes, []))
        key = self.api_key(self.user, ["library:read"])
        self.assertEqual(self.guard(chunk, headers=key["headers"])[0], self.chunk_bytes)
        for headers in ({}, {"Authorization": "Bearer nxs_unknown"}, {"Authorization": "Basic abc"},
                        {"Authorization": "Bearer nx_live_unknown"}, {"Authorization": "Bearer " + "x" * 500}):
            with self.subTest(headers=headers):
                limit, sent = self.guard(chunk, headers=headers)
                self.assertIsNone(limit)                    # answered here; the body is never asked for
                self.assertEqual([m["type"] for m in sent], ["http.response.start", "http.response.body"])
                self.assertEqual(sent[0]["status"], 401)
                self.assertIn(b"unauthorized", sent[1]["body"])
        self.ok(self.post("/auth/logout", {}, self.user))
        self.assertIsNone(self.guard(chunk, headers=self.user["headers"])[0])

    def test_a_revoked_key_no_longer_gets_the_chunk_bound(self):
        key = self.api_key(self.user, ["library:read", "library:write"])
        chunk = f"{API}/library/uploads/{UPLOAD}/chunks/0"
        self.assertEqual(self.guard(chunk, headers=key["headers"])[0], self.chunk_bytes)
        self.ok(self.delete(f"/api-keys/{key['id']}", self.user))
        self.assertIsNone(self.guard(chunk, headers=key["headers"])[0])


class RoomTest(LibraryCase):
    library_overrides = {"library_min_free_bytes": 1000, "library_uploads_per_hour": 4, "library_org_max_materials": 2}

    def setUp(self):
        super().setUp()
        self.user = self.register("dev@room.test")

    def declare(self, user, filename: str, size: int, **fields):
        return self.post("/library/uploads", {"filename": filename, "size": size, "sha256": "ab" * 32, **LABELS, **fields}, user)

    def test_refused_attempts_count_towards_the_hourly_limit(self):
        for index in range(4):
            self.error(self.start_raw(self.user, f"tool{index}.exe", b"MZ"), 415, "unsupported_type")
        self.error(self.start_raw(self.user, "spec.pdf", lib.pdf_bytes()), 429, "rate_limited")

    def test_the_volume_counts_what_other_organisations_have_yet_to_write(self):
        other = self.register("other@room.test")
        with mock.patch.object(LibraryStore, "free_bytes", return_value=10_000):
            self.ok(self.declare(self.user, "one.zip", 6000), 201)
            # 10 000 free − 6 000 promised to the first upload − 3 500 would leave 500: below the floor of 1 000.
            self.error(self.declare(other, "two.zip", 3500), 507, "storage_full")
            self.ok(self.declare(other, "two.zip", 3000), 201)
            self.error(self.declare(other, "three.zip", 1), 507, "storage_full")

    def test_a_chunk_is_refused_when_the_volume_has_filled_up_meanwhile(self):
        data = lib.pdf_bytes()
        upload = self.start(self.user, "spec.pdf", data)
        with mock.patch.object(LibraryStore, "free_bytes", return_value=1000 + len(data) - 1):
            self.error(self.put_chunk(self.user, upload["id"], 0, data), 507, "storage_full")
        self.assertEqual(self.stored_files(), [])
        self.ok(self.put_chunk(self.user, upload["id"], 0, data))        # room again: the upload goes on
        self.ok(self.complete(self.user, upload["id"]), 201)

    def test_the_number_of_materials_per_organisation_is_bounded(self):
        self.upload(self.user, "a.pdf", lib.pdf_bytes("a"))
        self.start(self.user, "b.pdf", lib.pdf_bytes("b"))                # an open upload counts as one to come
        body = self.error(self.start_raw(self.user, "c.pdf", lib.pdf_bytes("c")), 409, "library_quota_exceeded")
        self.assertEqual(body["max_materials"], 2)


class StuckUploadTest(LibraryCase):
    library_overrides = {"library_max_concurrent_completions": 1}

    def setUp(self):
        super().setUp()
        self.user = self.register("dev@stuck.test")
        self.data = lib.text_bytes(2 * self.chunk_bytes + 77)

    def send_all(self, upload):
        for index, part in enumerate(self.parts(upload, self.data)):
            self.ok(self.put_chunk(self.user, upload["id"], index, part))

    def set_upload(self, upload_id: str, **values):
        with self.db.transaction() as s:
            s.execute(update(MaterialUpload).where(MaterialUpload.id == upload_id).values(**values))

    def test_only_a_few_files_are_verified_at_a_time(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.send_all(upload)
        slots = self.app.state.library_store.verify_slots
        self.assertTrue(slots.acquire(blocking=False))                    # another completion is running
        try:
            body = self.error(self.complete(self.user, upload["id"]), 503, "library_busy")
            self.assertGreater(body["retry_after"], 0)
        finally:
            slots.release()
        self.assertEqual(self.ok(self.get(f"/library/uploads/{upload['id']}", self.user))["status"], "open")
        self.ok(self.complete(self.user, upload["id"]), 201)

    def test_an_upload_stuck_in_its_last_step_is_continued_not_sent_again(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.send_all(upload)
        self.set_upload(upload["id"], status="assembling")                # the process died while verifying
        again = self.start(self.user, "long.txt", self.data, expect=200)
        self.assertEqual((again["id"], again["resumed"], again["status"]), (upload["id"], True, "assembling"))
        self.assertEqual(again["received_count"], again["chunk_count"])
        self.error(self.put_chunk(self.user, upload["id"], 0, self.parts(upload, self.data)[0]), 409, "upload_not_open")
        material = self.ok(self.complete(self.user, upload["id"]), 201)
        self.assertEqual(self.content(self.user, material["id"]).content, self.data)

    def test_an_upload_that_can_take_no_more_chunks_makes_way_for_a_fresh_one(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.ok(self.put_chunk(self.user, upload["id"], 0, self.parts(upload, self.data)[0]))
        self.set_upload(upload["id"], chunk_attempts=10_000)
        self.error(self.put_chunk(self.user, upload["id"], 1, self.parts(upload, self.data)[1]), 429, "chunk_attempts_exceeded")
        fresh = self.start(self.user, "long.txt", self.data)              # 201: a new upload, not the dead one
        self.assertNotEqual(fresh["id"], upload["id"])
        self.assertEqual((fresh["resumed"], fresh["received"]), (False, []))
        self.error(self.get(f"/library/uploads/{upload['id']}", self.user), 404)
        self.assertEqual(self.stored_files(), [])
        self.send_all(fresh)
        self.ok(self.complete(self.user, fresh["id"]), 201)

    def test_an_upload_cannot_be_kept_alive_for_ever(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.set_upload(upload["id"], created_at=time.time() - library.MAX_UPLOAD_AGE_SECONDS - 60)
        self.ok(self.put_chunk(self.user, upload["id"], 0, self.parts(upload, self.data)[0]))   # still inside its idle limit
        self.error(self.get(f"/library/uploads/{upload['id']}", self.user), 404)               # … but past its maximum age
        self.start(self.user, "other.txt", b"hello\n")
        self.assertEqual(self.stored_files(), [])

    def test_finishing_twice_or_cancelling_afterwards_is_refused(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.send_all(upload)
        material = self.ok(self.complete(self.user, upload["id"]), 201)
        self.error(self.complete(self.user, upload["id"]), 404)
        self.error(self.delete(f"/library/uploads/{upload['id']}", self.user), 404)
        self.assertEqual(self.content(self.user, material["id"]).content, self.data)

    def test_continuing_takes_the_description_given_now(self):
        self.start(self.user, "long.txt", self.data, description="旧说明")
        again = self.start(self.user, "long.txt", self.data, expect=200, description="新说明")
        self.assertEqual(again["description"], "新说明")

    def test_a_chunk_that_fails_its_check_does_not_take_away_an_upload_being_completed(self):
        upload = self.start(self.user, "long.txt", self.data)
        self.send_all(upload)
        self.set_upload(upload["id"], status="assembling")
        problem = library.unsupported("内容不符")
        library._reject(self.db, self.app.state.library_store, upload["id"], self.user["org_id"], "user:x", problem,
                        only_if_open=True)
        with self.db.transaction() as s:
            self.assertIsNotNone(s.get(MaterialUpload, upload["id"]))
        self.assertEqual(len(self.stored_files()), 1)
        self.ok(self.complete(self.user, upload["id"]), 201)


class LeftoverTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.user = self.register("dev@leftover.test")
        self.store = LibraryStore(self.library_dir)

    def plant(self, org_id: str, data: bytes, age: float, digest: str | None = None) -> str:
        """A file in the place of the object ``digest`` (default: the digest of ``data`` itself)."""
        path = self.store.object_path(org_id, digest or sha(data))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_stored_files_without_a_record_are_removed_once_old(self):
        kept = self.upload(self.user, "spec.pdf", lib.pdf_bytes())
        kept_path = self.store.object_path(self.user["org_id"], kept["sha256"])
        os.utime(kept_path, (1, 1))                                       # old, but a material refers to it
        old = self.plant(self.user["org_id"], b"left behind by a crash", 2 * library.ORPHAN_AGE_SECONDS)
        fresh = self.plant(self.user["org_id"], b"being stored right now", 0)
        counts = library.sweep(self.db, self.settings, scan_objects=True)
        self.assertEqual(counts["library_orphan_files"], 1)
        self.assertEqual([os.path.exists(p) for p in (kept_path, old, fresh)], [True, False, True])
        self.assertEqual(self.content(self.user, kept["id"]).content, lib.pdf_bytes())

    def test_the_regular_sweep_drops_expired_uploads_and_staging_files_nobody_owns(self):
        data = lib.text_bytes(2 * self.chunk_bytes)
        expired = self.start(self.user, "old.txt", data)
        self.ok(self.put_chunk(self.user, expired["id"], 0, self.parts(expired, data)[0]))
        idle = self.start(self.user, "idle.txt", data + b"x\n")
        self.ok(self.put_chunk(self.user, idle["id"], 0, self.parts(idle, data + b"x\n")[0]))
        with self.db.transaction() as s:
            s.execute(update(MaterialUpload).where(MaterialUpload.id == expired["id"]).values(expires_at=1.0))
        staging = os.path.join(self.library_dir, "staging")
        orphan = os.path.join(staging, "upl_" + "f" * 20 + ".part")
        with open(orphan, "wb") as handle:
            handle.write(b"no upload knows this file")
        for name in os.listdir(staging):                                  # everything looks long untouched
            os.utime(os.path.join(staging, name), (1, 1))
        counts = maintenance.sweep(self.db, self.settings)
        self.assertEqual(counts["library_uploads_expired"], 1)
        self.assertEqual(os.listdir(staging), [idle["id"] + ".part"])     # the idle upload is alive: its file stays
        self.assertEqual(self.ok(self.get(f"/library/uploads/{idle['id']}", self.user))["received"], [0])
        with self.db.transaction() as s:
            self.assertEqual(s.execute(select(MaterialUpload.id)).scalars().all(), [idle["id"]])

    def test_a_file_swapped_under_its_name_is_not_stored(self):
        digest = sha(b"verified content")
        with self.store.staging(UPLOAD, exclusive=True, create=True) as staging:
            staging.write(0, b"verified content")
            os.unlink(staging.path)                                       # someone removed it …
            with open(staging.path, "wb") as handle:                      # … and something else took the name
                handle.write(b"not what was verified")
            self.assertFalse(staging.is_current())
            with self.assertRaises(StagingLost):
                self.store.promote(staging, self.user["org_id"], digest)
        self.assertIsNone(self.store.object_size(self.user["org_id"], digest))
        with self.store.staging(UPLOAD, exclusive=True, create=False) as staging:
            os.unlink(staging.path)
            with self.assertRaises(StagingLost):
                self.store.promote(staging, self.user["org_id"], digest)

    def test_a_damaged_stored_file_is_replaced_by_the_verified_one(self):
        data = b"the right content"
        damaged = self.plant(self.user["org_id"], b"x" * len(data), 0, sha(data))   # same size, other bytes
        self.assertFalse(self.store.object_matches(self.user["org_id"], sha(data)))
        with self.store.staging(UPLOAD, exclusive=True, create=True) as staging:
            staging.write(0, data)
            self.store.promote(staging, self.user["org_id"], sha(data), keep_existing=False)
        with open(damaged, "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertTrue(self.store.object_matches(self.user["org_id"], sha(data)))
        with self.store.staging(UPLOAD, exclusive=True, create=True) as staging:
            staging.write(0, data)
            self.store.promote(staging, self.user["org_id"], sha(data), keep_existing=True)
        self.assertEqual(self.stored_files(), [os.path.relpath(damaged, self.library_dir)])
