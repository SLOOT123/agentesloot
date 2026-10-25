"""API privada real; cola asíncrona y proveedor controlados por eventos de prueba."""
import asyncio
import shutil
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import test_project as fixtures

from agent_core.team_control import TeamCheckpoints


class MultiAgentApiTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    upstream = fixtures.ProjectTest.upstream
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        shutil.copytree(fixtures.PROJECT / "agent_skills", self.root / "agent_skills", dirs_exist_ok=True)
        config = self.config()
        config.setdefault("multi_agent", {})["enabled"] = False
        self.save_config(config)

    def submit(self, headers, conversation, message="hola", identifier=None):
        return self.client.post(f"/api/agent/conversations/{conversation}/messages", headers=headers,
                                json={"message": message, "request_id": identifier or str(uuid.uuid4()), "background": True})

    def wait_job(self, conversation, identifier):
        route = f"/api/agent/conversations/{conversation}/jobs/{identifier}"
        for _ in range(200):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()["status"] in {"complete", "cancelled", "failed"}:
                return response.json()
            time.sleep(.01)
        self.fail("El trabajo de prueba no alcanzó un estado terminal")

    def test_background_returns_before_provider_and_repeated_request_finishes_one_turn(self):
        headers = self.register()
        conversation = self.conversation(headers)
        identifier = str(uuid.uuid4())
        original = self.app.state.agent.respond
        release, entered = asyncio.Event(), threading.Event()
        async def responding(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        runner = AsyncMock(side_effect=responding)
        with patch.object(self.app.state.agent, "respond", runner):
            first = self.submit(headers, conversation, "ola", identifier)
            self.assertIn(first.status_code, (200, 202), first.text)
            self.assertTrue(entered.wait(1))
            self.assertIn(first.json()["status"], {"queued", "running"})
            self.assertEqual(self.requests, [])
            self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/messages").json(), [])
            duplicate = self.submit(headers, conversation, "ola", identifier)
            self.assertEqual(duplicate.json()["job"]["id"], first.json()["job"]["id"])
            pending = self.client.get(f"/api/agent/conversations/{conversation}/jobs").json()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["prompt"], "ola")
            self.assertEqual(pending[0]["request_id"], identifier)
            self.assertIn(pending[0]["status"], {"queued", "running"})
            self.client.portal.call(release.set)
            completed = self.wait_job(conversation, identifier)
            self.assertEqual(completed["status"], "complete", completed)
            self.assertIn("Respuesta SIMULADA", completed["result"]["answer"])
            self.assertEqual(runner.await_count, 1)
            self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conversation}/messages").json()), 2)
            replay = self.submit(headers, conversation, "ola", identifier)
            self.assertEqual(replay.json()["job"]["result"], completed["result"])
            self.assertEqual(runner.await_count, 1)

    def test_cancellation_propagates_and_reloading_jobs_keeps_original_prompt(self):
        headers = self.register()
        conversation = self.conversation(headers)
        identifier = str(uuid.uuid4())
        entered, cancelled = threading.Event(), threading.Event()
        release = asyncio.Event()
        async def responding(*args, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with patch.object(self.app.state.agent, "respond", AsyncMock(side_effect=responding)):
            response = self.submit(headers, conversation, "Consulta conservada al recargar", identifier)
            self.assertIn(response.status_code, (200, 202), response.text)
            self.assertTrue(entered.wait(1))
            base = f"/api/agent/conversations/{conversation}/jobs/{identifier}"
            self.assertEqual(self.client.post(base + "/cancel").status_code, 403)
            self.assertEqual(self.client.post(base + "/cancel", headers={"X-CSRF-Token": "incorrecto"}).status_code, 403)
            stopped = self.client.post(base + "/cancel", headers=headers)
            self.assertEqual(stopped.status_code, 200, stopped.text)
            self.assertEqual(stopped.json()["status"], "cancelled")
            self.assertTrue(cancelled.wait(1))
            restored = self.client.get(f"/api/agent/conversations/{conversation}/jobs").json()
            self.assertEqual(restored[0]["request_id"], identifier)
            self.assertEqual(restored[0]["status"], "cancelled")
            self.assertEqual(restored[0]["prompt"], "Consulta conservada al recargar")
            self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/messages").json(), [])

    def test_jobs_and_team_artifacts_require_session_and_conversation_ownership(self):
        headers = self.register()
        conversation = self.conversation(headers)
        identifier = str(uuid.uuid4())
        reply = self.submit(headers, conversation, "hola", identifier)
        self.assertIn(reply.status_code, (200, 202), reply.text)
        self.assertEqual(self.wait_job(conversation, identifier)["status"], "complete")
        database = self.app.state.database
        with database.connection() as connection:
            user_id = connection.execute("SELECT user_id FROM agent_conversations WHERE id=?", (conversation,)).fetchone()[0]
        skill = {"id": "research", "version": "1.0.0"}
        checkpoint = TeamCheckpoints(database, user_id, conversation, identifier, "hola", skill)
        checkpoint("research:worker", {"evidence": "EVIDENCIA_PRIVADA_DE_ALICE"})
        checkpoint_other_request = TeamCheckpoints(database, user_id, conversation, str(uuid.uuid4()), "Otra solicitud", skill)
        checkpoint_other_request("research:worker", {"evidence": "OTRA_SOLICITUD_NO_MEZCLAR"})
        base = f"/api/agent/conversations/{conversation}"
        artifacts = self.client.get(base + f"/team-artifacts/{identifier}")
        self.assertEqual(artifacts.status_code, 200, artifacts.text)
        self.assertEqual(len(artifacts.json()["artifacts"]), 1)
        self.assertEqual(artifacts.json()["artifacts"][0]["payload"], {"evidence": "EVIDENCIA_PRIVADA_DE_ALICE"})
        self.assertNotIn("OTRA_SOLICITUD_NO_MEZCLAR", artifacts.text)
        self.client.cookies.clear()
        for route in ("/jobs", f"/jobs/{identifier}", f"/team-artifacts/{identifier}"):
            self.assertEqual(self.client.get(base + route).status_code, 401)
        self.assertEqual(self.client.post(base + f"/jobs/{identifier}/cancel").status_code, 401)
        bob = self.register("bob")
        for route in ("/jobs", f"/jobs/{identifier}", f"/team-artifacts/{identifier}"):
            denied = self.client.get(base + route)
            self.assertEqual(denied.status_code, 404, denied.text)
            self.assertNotIn("EVIDENCIA_PRIVADA_DE_ALICE", denied.text)
        self.assertEqual(self.client.post(base + f"/jobs/{identifier}/cancel", headers=bob).status_code, 404)

    def test_background_message_requires_csrf_and_synchronous_api_remains_compatible(self):
        headers = self.register()
        conversation = self.conversation(headers)
        self.assertEqual(self.submit({}, conversation).status_code, 403)
        self.assertEqual(self.submit({"X-CSRF-Token": "forjado"}, conversation).status_code, 403)
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/jobs").json(), [])
        response = self.send(headers, conversation, "hola")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("answer", response.json())
        self.assertNotIn("job", response.json())
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conversation}/messages").json()), 2)
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/jobs").json(), [])


if __name__ == "__main__":
    unittest.main()
