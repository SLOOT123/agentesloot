"""Concurrencia real de FastAPI/colas, con proveedor HTTPX simulado y bloqueado."""
import asyncio
import shutil
import threading
import time
import unittest
import uuid

import test_project as fixtures


class TeamSchedulingTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation

    def setUp(self):
        self.active, self.peak = 0, 0
        self.block_first, self.blocked = False, False
        self.release = asyncio.Event()
        self.cancelled = threading.Event()
        fixtures.ProjectTest.setUp(self)
        shutil.copytree(fixtures.PROJECT / "agent_skills", self.root / "agent_skills", dirs_exist_ok=True)
        config = self.config()
        config.setdefault("multi_agent", {}).update(enabled=True, max_parallel_model_calls=1)
        self.save_config(config)

    async def upstream(self, request):
        tracked = request.url.path in {"/api/show", "/api/chat", "/v1/chat/completions"}
        if not tracked:
            return await fixtures.ProjectTest.upstream(self, request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.last_model_path = request.url.path
        try:
            if self.block_first and not self.blocked:
                self.blocked = True
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            # Permite detectar solapamientos si desaparece cualquier gate.
            await asyncio.sleep(.01)
            return await fixtures.ProjectTest.upstream(self, request)
        finally:
            self.active -= 1

    def public(self, headers, message="hola público"):
        response = self.client.post("/api/messages", headers=headers,
                                    json={"message": message, "request_id": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["message_id"]

    def private(self, headers, conversation, message="hola privado"):
        identifier = str(uuid.uuid4())
        response = self.client.post(f"/api/agent/conversations/{conversation}/messages", headers=headers,
                                    json={"message": message, "request_id": identifier, "background": True})
        self.assertIn(response.status_code, (200, 202), response.text)
        return identifier

    def wait_until(self, predicate, description):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.01)
        self.fail(description)

    def wait_private(self, conversation, identifier):
        def terminal():
            response = self.client.get(f"/api/agent/conversations/{conversation}/jobs/{identifier}")
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            return value if value["status"] in {"complete", "failed", "cancelled"} else None
        return self.wait_until(terminal, "La respuesta privada quedó bloqueada esperando el modelo")

    def wait_public(self, identifier):
        def terminal():
            messages = self.client.get("/api/messages").json()
            item = next(item for item in messages if item["id"] == identifier)
            return item if item["assistant_status"] in {"complete", "failed"} else None
        return self.wait_until(terminal, "La respuesta pública quedó bloqueada esperando el modelo")

    def wait_private_running(self, conversation, identifier):
        def started():
            return self.client.get(f"/api/agent/conversations/{conversation}/activity",
                                   params={"request_id": identifier}).json().get("status") == "running"
        self.wait_until(started, "El trabajo privado no reservó su ejecución")

    def test_public_and_private_greetings_share_one_model_slot_including_inspection(self):
        headers = self.register()
        conversation = self.conversation(headers, "general")
        self.block_first = True
        public = self.public(headers)
        self.assertTrue(self.entered.wait(1))
        private = self.private(headers, conversation)
        self.wait_private_running(conversation, private)
        self.assertEqual(self.active, 1)
        self.assertEqual(self.peak, 1)
        self.client.portal.call(self.release.set)
        self.assertEqual(self.wait_public(public)["assistant_status"], "complete")
        self.assertEqual(self.wait_private(conversation, private)["status"], "complete")
        self.assertEqual(self.peak, 1)
        self.assertEqual(self.active, 0)
        paths = [path for path, _, _ in self.requests]
        self.assertIn("/api/show", paths)
        self.assertGreaterEqual(paths.count("/api/chat"), 2)
        private_messages = self.client.get(f"/api/agent/conversations/{conversation}/messages").json()
        public_messages = self.client.get("/api/messages").json()
        self.assertEqual(len(private_messages), 2)
        self.assertEqual(len(public_messages), 2)
        self.assertNotIn("hola privado", public_messages[1]["message"])

    def test_cancelling_private_inspection_releases_the_slot_for_public_and_next_private(self):
        headers = self.register()
        conversation = self.conversation(headers, "general")
        self.block_first = True
        private = self.private(headers, conversation)
        self.assertTrue(self.entered.wait(1))
        public = self.public(headers)
        self.assertEqual(self.active, 1)
        stopped = self.client.post(f"/api/agent/conversations/{conversation}/jobs/{private}/cancel", headers=headers)
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(stopped.json()["status"], "cancelled")
        self.assertTrue(self.cancelled.wait(1))
        # El evento sigue cerrado: el progreso depende de liberar el gate al cancelar.
        self.assertFalse(self.release.is_set())
        self.assertEqual(self.wait_public(public)["assistant_status"], "complete")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/messages").json(), [])
        next_private = self.private(headers, conversation, "hola otra vez")
        self.assertEqual(self.wait_private(conversation, next_private)["status"], "complete")
        self.assertEqual(self.peak, 1)
        self.assertEqual(self.active, 0)

    def test_private_executor_holds_the_same_slot_after_capabilities_are_cached(self):
        headers = self.register()
        conversation = self.conversation(headers, "general")
        self.client.portal.call(self.app.state.agent.check, "local")
        self.assertEqual(self.active, 0)
        self.requests.clear()
        self.peak = 0
        self.entered.clear()
        self.block_first = True
        private = self.private(headers, conversation)
        self.assertTrue(self.entered.wait(1))
        self.assertEqual(self.last_model_path, "/api/chat")
        public = self.public(headers)
        def public_started():
            original = next(item for item in self.client.get("/api/messages").json() if item["id"] == public)
            return original["assistant_status"] == "running"
        self.wait_until(public_started, "La respuesta pública no esperó al ejecutor privado")
        self.assertEqual(self.active, 1)
        self.assertEqual(self.peak, 1)
        self.client.portal.call(self.release.set)
        self.assertEqual(self.wait_private(conversation, private)["status"], "complete")
        self.assertEqual(self.wait_public(public)["assistant_status"], "complete")
        self.assertNotIn("/api/show", [path for path, _, _ in self.requests])
        self.assertEqual(self.peak, 1)
        self.assertEqual(self.active, 0)

    def test_cancelling_public_model_call_releases_private_and_public_recovery_does_not_deadlock(self):
        headers = self.register()
        conversation = self.conversation(headers, "general")
        self.block_first = True
        public = self.public(headers)
        self.assertTrue(self.entered.wait(1))
        private = self.private(headers, conversation)
        self.wait_private_running(conversation, private)
        self.client.portal.call(self.app.state.community_agent.close)
        self.assertTrue(self.cancelled.wait(1))
        self.assertFalse(self.release.is_set())
        self.assertEqual(self.wait_private(conversation, private)["status"], "complete")
        with self.app.state.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT status FROM community_replies WHERE message_id=?", (public,)).fetchone()[0], "queued")
        self.client.portal.call(self.app.state.community_agent.start)
        self.assertEqual(self.wait_public(public)["assistant_status"], "complete")
        self.assertEqual(len(self.client.get("/api/messages").json()), 2)
        self.assertEqual(self.peak, 1)
        self.assertEqual(self.active, 0)


if __name__ == "__main__":
    unittest.main()
