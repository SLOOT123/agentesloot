"""Equipo público real con investigadores simulados y aislamiento del contexto."""
import json
import shutil
import unittest
from unittest.mock import AsyncMock, patch

import test_project as fixtures
import test_community as community_fixtures
from agent_core.config import AgentError


class PublicTeamTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    upstream = fixtures.ProjectTest.upstream
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    post = community_fixtures.CommunityTest.post
    wait_reply = community_fixtures.CommunityTest.wait_reply

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        shutil.copytree(fixtures.PROJECT / "agent_skills", self.root / "agent_skills", dirs_exist_ok=True)
        config = self.config()
        config["multi_agent"]["enabled"] = True
        self.save_config(config)

    def test_public_team_uses_only_public_history_and_fresh_worker_tools(self):
        headers = self.register()
        conversation = self.conversation(headers)
        self.assertEqual(self.send(headers, conversation, "HISTORIAL_PRIVADO_CANARIO").status_code, 200)
        self.client.post("/api/agent/memory", headers=headers,
                         json={"title": "MEMORIA_PRIVADA_CANARIO", "content": "DOCUMENTO_PRIVADO_CANARIO"})
        self.app.state.database.post_community("alice", "ANTECEDENTE_PUBLICO")
        self.requests.clear()
        captured = {}
        expected_sources = [
            {"id": 1, "kind": "web", "title": "Referencia pública A", "url": "https://docs.example.org/a", "retrieval": "page_extract"},
            {"id": 2, "kind": "web", "title": "Referencia pública B", "url": "https://reference.example.net/b", "retrieval": "page_extract"},
        ]

        async def research(team, message, history):
            captured.update(message=message, history=history, skill=team.skill)
            first, second = team.tools_factory("investigador-a"), team.tools_factory("investigador-b")
            captured["workers"] = (first, second)
            captured["initial_sources"] = [list(first.sources), list(second.sources)]
            first.add_sources([expected_sources[0]])
            captured["second_sources_after_first_write"] = list(second.sources)
            team.checkpointer("public-test-evidence", {"sources": expected_sources})
            team.event("source_read", "Fuente compartida consultada", {"source_id": 1})
            return {"answer": "RESULTADO_PUBLICO_CON_FUENTES [1] [2]", "sources": expected_sources,
                    "team": {"status": "complete"}}

        question = "Investiga referencias públicas de este enlace https://docs.example.org/inicio"
        with patch("agent_core.team_research.TeamResearch.run", new=research):
            response = self.post(headers, question)
            self.assertEqual(response.status_code, 200, response.text)
            original, messages = self.wait_reply(response.json()["message_id"])
        self.assertEqual(original["assistant_status"], "complete")
        self.assertEqual(captured["message"], question)
        self.assertEqual(captured["history"], [{"role": "user", "content": "alice: ANTECEDENTE_PUBLICO"}])
        self.assertEqual(captured["skill"]["id"], "research")
        first, second = captured["workers"]
        self.assertIsNot(first, second)
        self.assertEqual(first.user_id, second.user_id)
        with self.app.state.database.connection() as connection:
            owner = connection.execute("SELECT id FROM users WHERE username='alice'").fetchone()[0]
        self.assertEqual(first.user_id, owner)
        for worker in (first, second):
            self.assertIsNone(worker.conversation_id)
            self.assertIsNone(worker.project)
            self.assertIsNone(worker.downloader)
            self.assertFalse(worker.learning)
        for source_list in captured["initial_sources"]:
            self.assertEqual([source["url"] for source in source_list], ["https://docs.example.org/inicio"])
        self.assertEqual(captured["initial_sources"][1], captured["second_sources_after_first_write"])
        answer = next(item for item in messages if item["role"] == "assistant")
        self.assertEqual(answer["sources"], expected_sources)
        self.assertEqual(answer["team_status"], "complete")
        self.assertEqual(answer["reply_to"], original["id"])
        self.assertEqual(self.requests, [], "La investigación simulada no debe llamar a otro modelo")
        with self.app.state.database.connection() as connection:
            saved = connection.execute("SELECT sources_json,team_status FROM messages WHERE id=?", (answer["id"],)).fetchone()
            checkpoint = connection.execute("SELECT message_id,payload FROM community_team_checkpoints WHERE phase='public-test-evidence'").fetchone()
        self.assertEqual(json.loads(saved["sources_json"]), expected_sources)
        self.assertEqual(saved["team_status"], "complete")
        self.assertEqual(checkpoint["message_id"], original["id"])
        self.assertNotIn("PRIVADO_CANARIO", json.dumps([captured["history"], messages, checkpoint["payload"]]))

        # Las evidencias públicas se comparten; el historial privado conserva su propietario.
        self.client.cookies.clear()
        self.register("bob")
        shared = self.client.get("/api/messages").json()
        self.assertEqual(next(item for item in shared if item["id"] == answer["id"])["sources"], expected_sources)
        self.assertNotIn("PRIVADO_CANARIO", json.dumps(shared))
        self.assertEqual(self.client.get("/api/agent/conversations").json(), [])
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/messages").status_code, 404)
        self.assertEqual(self.client.get("/api/agent/memory").json(), [])

    def test_greeting_with_team_enabled_still_uses_one_text_call(self):
        headers = self.register()
        self.client.post("/api/agent/memory", headers=headers,
                         json={"title": "SECRETO_CANARIO", "content": "DOCUMENTO_PRIVADO_CANARIO"})
        with patch("agent_core.team_research.TeamResearch.run", new=AsyncMock()) as research:
            response = self.post(headers, "ola")
            original, messages = self.wait_reply(response.json()["message_id"])
        self.assertEqual(original["assistant_status"], "complete")
        research.assert_not_awaited()
        self.assertEqual(len(self.requests), 1)
        body = self.requests[0][1]
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "alice: ola"})
        self.assertNotIn("PRIVADO_CANARIO", json.dumps(body))
        answer = next(item for item in messages if item["role"] == "assistant")
        self.assertEqual(answer["sources"], [])
        self.assertIsNone(answer["team_status"])

    def test_team_failure_keeps_public_request_without_claiming_a_result(self):
        headers = self.register()
        async def failure(*args):
            raise AgentError("team_test_failed", "No se pudo verificar la investigación simulada.", 502)
        with patch("agent_core.team_research.TeamResearch.run", new=failure):
            response = self.post(headers, "Investiga fuentes sobre pruebas automáticas")
            original, messages = self.wait_reply(response.json()["message_id"])
        self.assertEqual(original["assistant_status"], "failed")
        self.assertIn("No se pudo verificar", original["assistant_error"])
        self.assertEqual([item["role"] for item in messages], ["user"])
        self.assertEqual(original["sources"], [])
        self.assertFalse(self.requests)
