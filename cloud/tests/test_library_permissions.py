"""Issue #33: the library is kept per organisation and uses the organisation roles.
owner / admin / developer upload and delete; a field operator only reads. Every
request is authenticated, each chunk included."""
import library_support as lib
from library_support import LABELS, OCTET, LibraryCase, sha
from support import API


class PermissionTest(LibraryCase):
    def setUp(self):
        super().setUp()
        self.owner = self.register("owner@library.test")
        self.data = lib.text_bytes(2 * self.chunk_bytes + 100)

    def test_every_route_needs_a_login_each_chunk_included(self):
        upload = self.start(self.owner, "long.txt", self.data)
        material = self.upload(self.owner, "spec.pdf", lib.pdf_bytes())
        part = self.parts(upload, self.data)[0]
        body = {"filename": "a.pdf", "size": 10, "sha256": sha(b"0123456789"), **LABELS}
        anonymous = [self.client.get(f"{API}/library/formats"),
                     self.client.get(f"{API}/library/materials"),
                     self.client.get(f"{API}/library/materials/{material['id']}"),
                     self.client.get(f"{API}/library/materials/{material['id']}/content"),
                     self.client.delete(f"{API}/library/materials/{material['id']}"),
                     self.client.post(f"{API}/library/uploads", json=body),
                     self.client.get(f"{API}/library/uploads/{upload['id']}"),
                     self.client.put(f"{API}/library/uploads/{upload['id']}/chunks/0", content=part,
                                     headers={"Content-Type": OCTET}),
                     self.client.post(f"{API}/library/uploads/{upload['id']}/complete", json={}),
                     self.client.delete(f"{API}/library/uploads/{upload['id']}")]
        for response in anonymous:
            self.error(response, 401)
        forged = {"Authorization": "Bearer nxs_not-a-real-session"}
        self.error(self.put_chunk(None, upload["id"], 0, part, headers=forged), 401)
        self.assertEqual(self.ok(self.get(f"/library/uploads/{upload['id']}", self.owner))["received"], [])

    def test_another_organisation_sees_nothing_and_cannot_touch_anything(self):
        other = self.register("other@elsewhere.test")
        upload = self.start(self.owner, "long.txt", self.data)
        part = self.parts(upload, self.data)[0]
        material = self.upload(self.owner, "spec.pdf", lib.pdf_bytes())
        self.assertEqual(self.ok(self.get("/library/materials", other))["materials"], [])
        self.error(self.get(f"/library/materials/{material['id']}", other), 404)
        self.error(self.content(other, material["id"]), 404)
        self.error(self.delete(f"/library/materials/{material['id']}", other), 404)
        self.error(self.get(f"/library/uploads/{upload['id']}", other), 404)
        self.error(self.put_chunk(other, upload["id"], 0, part), 404)
        self.error(self.complete(other, upload["id"]), 404)
        self.error(self.delete(f"/library/uploads/{upload['id']}", other), 404)
        # Asking for someone else's organisation by header is refused outright.
        headers = {**other["headers"], "X-NeuxMind-Org": self.owner["org_id"]}
        self.error(self.client.get(f"{API}/library/materials", headers=headers), 403, "not_member")
        # The same file in another organisation is that organisation's own copy.
        mine = self.upload(other, "spec.pdf", lib.pdf_bytes())
        self.assertNotEqual(mine["id"], material["id"])
        self.assertEqual(self.ok(self.get("/library/materials", self.owner))["total"], 1)
        self.assertEqual(self.ok(self.get(f"/library/uploads/{upload['id']}", self.owner))["received"], [])

    def test_a_field_operator_reads_but_does_not_upload_or_delete(self):
        material = self.upload(self.owner, "spec.pdf", lib.pdf_bytes())
        operator = self.member(self.owner, "field@library.test", "operator")
        self.assertEqual(self.ok(self.get("/library/materials", operator))["total"], 1)
        self.assertEqual(self.ok(self.get(f"/library/materials/{material['id']}", operator))["sha256"], material["sha256"])
        self.assertEqual(self.content(operator, material["id"]).content, lib.pdf_bytes())
        self.ok(self.get("/library/formats", operator))
        self.error(self.start_raw(operator, "more.pdf", lib.pdf_bytes("more")), 403, "forbidden")
        self.error(self.delete(f"/library/materials/{material['id']}", operator), 403, "forbidden")
        self.assertEqual(self.ok(self.get("/library/materials", self.owner))["total"], 1)

    def test_developers_and_admins_upload_and_delete(self):
        for role in ("developer", "admin"):
            with self.subTest(role):
                member = self.member(self.owner, f"{role}@library.test", role)
                material = self.upload(member, f"{role}.pdf", lib.pdf_bytes(role))
                self.assertEqual(material["created_by"], member["user"]["id"])
                self.assertTrue(self.ok(self.delete(f"/library/materials/{material['id']}", member))["deleted"])

    def test_only_the_person_who_started_an_upload_continues_it(self):
        colleague = self.member(self.owner, "colleague@library.test", "developer")
        upload = self.start(self.owner, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.error(self.get(f"/library/uploads/{upload['id']}", colleague), 404)
        self.error(self.put_chunk(colleague, upload["id"], 0, parts[0]), 404)
        self.error(self.complete(colleague, upload["id"]), 404)
        self.error(self.delete(f"/library/uploads/{upload['id']}", colleague), 404)
        self.assertEqual(self.ok(self.get(f"/library/uploads/{upload['id']}", self.owner))["received"], [])

    def test_losing_the_role_stops_an_upload_at_the_next_chunk(self):
        developer = self.member(self.owner, "dev@library.test", "developer")
        upload = self.start(developer, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.ok(self.put_chunk(developer, upload["id"], 0, parts[0]))
        self.ok(self.post("/org/members", {"email": "dev@library.test", "role": "operator"}, self.owner))
        self.error(self.put_chunk(developer, upload["id"], 1, parts[1]), 403, "forbidden")
        self.error(self.complete(developer, upload["id"]), 403, "forbidden")
        self.ok(self.delete(f"/org/members/{developer['user']['id']}", self.owner))
        self.error(self.put_chunk(developer, upload["id"], 1, parts[1]), 403, "not_member")

    def test_a_login_that_ended_stops_an_upload_at_the_next_chunk(self):
        upload = self.start(self.owner, "long.txt", self.data)
        parts = self.parts(upload, self.data)
        self.ok(self.put_chunk(self.owner, upload["id"], 0, parts[0]))
        self.ok(self.post("/auth/logout", {}, self.owner))
        self.error(self.put_chunk(self.owner, upload["id"], 1, parts[1]), 401)

    def test_an_api_key_needs_the_library_scopes(self):
        material = self.upload(self.owner, "spec.pdf", lib.pdf_bytes())
        robots_only = self.api_key(self.owner, ["robots"])
        self.error(self.get("/library/materials", robots_only), 403, "scope_missing")
        self.error(self.content(robots_only, material["id"]), 403, "scope_missing")
        reader = self.api_key(self.owner, ["library:read"])
        self.assertEqual(self.ok(self.get("/library/materials", reader))["total"], 1)
        self.assertEqual(self.content(reader, material["id"]).content, lib.pdf_bytes())
        self.error(self.start_raw(reader, "more.pdf", lib.pdf_bytes("more")), 403, "scope_missing")
        self.error(self.delete(f"/library/materials/{material['id']}", reader), 403, "scope_missing")
        writer = self.api_key(self.owner, ["library:read", "library:write"])
        uploaded = self.upload(writer, "by-key.pdf", lib.pdf_bytes("key"))
        self.assertEqual(uploaded["created_by"], self.owner["user"]["id"])

    def test_a_field_operator_cannot_mint_a_key_that_uploads(self):
        operator = self.member(self.owner, "field@library.test", "operator")
        self.assertIn("library:read", self.ok(self.get("/me", operator))["permissions"])
        self.assertNotIn("library:write", self.ok(self.get("/me", operator))["permissions"])

    def test_an_unverified_account_reads_but_cannot_upload(self):
        unverified = self.register("new@library.test", verify=False)
        self.assertEqual(self.ok(self.get("/library/materials", unverified))["total"], 0)
        self.error(self.start_raw(unverified, "spec.pdf", lib.pdf_bytes()), 403, "email_unverified")
        self.assertEqual(self.stored_files(), [])

    def test_uploads_and_deletions_are_audited(self):
        from sqlalchemy import select

        from neuxmind_cloud.models import AuditLog
        material = self.upload(self.owner, "spec.pdf", lib.pdf_bytes())
        self.ok(self.delete(f"/library/materials/{material['id']}", self.owner))
        with self.db.transaction() as s:
            actions = [row.action for row in s.execute(select(AuditLog).where(AuditLog.org_id == self.owner["org_id"],
                                                                              AuditLog.action.like("library.%"))).scalars()]
        self.assertEqual(actions, ["library.upload_start", "library.material_create", "library.material_delete"])
