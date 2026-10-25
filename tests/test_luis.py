"""Perfil real/API/SQLite; los servicios externos están simulados."""
import json
import shutil
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.presentation import LUIS_BANNER, LUIS_HEADER
from agent_core.providers import Provider
from agent_core.service import build_context
from agent_core.tools import SCHEMAS


class LuisProfileTest(unittest.TestCase):
    setUp = fixtures.ProjectTest.setUp
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    async def upstream(self, request):
        if request.url.path == "/api/chat" and json.loads(request.content).get("format"):
            self.requests.append((request.url.path, json.loads(request.content), dict(request.headers)))
            return httpx.Response(200, json={"message": {"content": json.dumps({"answer": "¿Qué tarea desea resolver?"}, ensure_ascii=False)}})
        return await fixtures.ProjectTest.upstream(self, request)

    def test_profile_is_listed_with_team_tools_and_structured_context_fits(self):
        headers = self.register()
        config = self.client.get("/api/agent/config").json()
        self.assertIn("luis", {item["id"] for item in config["profiles"]})
        files = self.app.state.files
        profile = files.profile("luis")
        self.assertEqual(profile.tools, files.profile("equipo").tools)
        prompt = files.prompt(profile)
        self.assertIn("Tu nombre de presentación es Luis", prompt)
        self.assertNotIn("no_self_authorized_exits", prompt)
        backend = files.agent().backends["local"]
        messages = build_context(prompt, [], "hola", [], [SCHEMAS["task_plan"]], backend.context_window, backend.max_output_tokens)
        body, _ = Provider(backend, files.agent().limits).structured_request(messages, [SCHEMAS["task_plan"]], allow_answer=True)
        self.assertLess(len(json.dumps(body, ensure_ascii=False).encode("utf-8")), backend.context_window - backend.max_output_tokens - 256)
        self.assertEqual(self.client.get(f"/api/agent/conversations/{self.conversation(headers, 'luis')}/messages").json(), [])

    def test_greeting_keeps_model_protocol_and_persists_one_formatted_answer(self):
        headers = self.register()
        self.mode = "tools"
        conversation = self.conversation(headers, "luis")
        identifier = str(uuid.uuid4())
        response = self.send(headers, conversation, "ola", identifier)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["answer"].startswith(LUIS_HEADER + "\n"))
        self.assertIn(LUIS_BANNER, result["answer"])
        self.assertIn("¿Qué tarea desea resolver?", result["answer"])
        self.assertEqual(result["answer"].count(LUIS_HEADER), 1)
        self.assertEqual(result["tools_used"], [])
        self.assertEqual(len([item for item in self.requests if item[0] == "/api/chat"]), 2)
        self.assertTrue(all(item[1].get("format") for item in self.requests if item[0] == "/api/chat"))
        history = self.client.get(f"/api/agent/conversations/{conversation}/messages").json()
        self.assertEqual(history[-1]["content"], result["answer"])
        self.assertEqual(self.send(headers, conversation, "ola", identifier).json(), result)
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conversation}/messages").json()), 2)

    def test_direct_team_research_keeps_sources_and_partial_status(self):
        headers = self.register()
        shutil.copytree(fixtures.PROJECT / "agent_skills", self.root / "agent_skills", dirs_exist_ok=True)
        config = self.config()
        config["multi_agent"]["enabled"] = True
        self.save_config(config)
        conversation = self.conversation(headers, "luis")
        sources = [{"id": 1, "kind": "web", "url": "https://example.org/documento", "title": "Fuente simulada", "excerpt": "Texto observado de prueba.", "retrieval": "page_extract"}]
        research = {"answer": "La investigación quedó parcial. Evidencia disponible [1]", "sources": sources,
                    "model_calls": 4, "tools_used": ["web_search", "web_read"], "review": "team_partial", "team": {"status": "partial", "skill_id": "research"}}
        with patch("agent_core.team_research.TeamResearch.run", AsyncMock(return_value=research)):
            response = self.send(headers, conversation, "Investiga este asunto con fuentes")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["answer"].startswith(LUIS_HEADER))
        self.assertIn(research["answer"], result["answer"])
        self.assertNotIn(LUIS_BANNER, result["answer"])
        self.assertEqual(result["sources"], sources)
        self.assertEqual(result["team"]["status"], "partial")

    def test_direct_download_keeps_queued_state_and_other_profiles_keep_their_style(self):
        headers = self.register()
        conversation = self.conversation(headers, "luis")
        queued = {"status": "queued", "job": {"id": str(uuid.uuid4()), "status": "queued"}}
        with patch.object(self.app.state.download_jobs, "submit", AsyncMock(return_value=queued)) as submit:
            response = self.send(headers, conversation, "Descarga el audio de https://example.org/video")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["answer"].startswith(LUIS_HEADER))
        self.assertIn("Continúa en segundo plano", result["answer"])
        self.assertEqual(result["review"], "pending_download")
        self.assertEqual(result["download_job"]["status"], "queued")
        self.assertEqual(result["model_calls"], 0)
        submit.assert_awaited_once()
        standard = self.send(headers, self.conversation(headers, "general"), "Hola agente")
        self.assertEqual(standard.status_code, 200, standard.text)
        self.assertFalse(standard.json()["answer"].startswith(LUIS_HEADER))


if __name__ == "__main__":
    unittest.main()
