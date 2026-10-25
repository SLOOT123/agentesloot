"""Perfil y cola reales con SQLite temporal; modelo e investigación simulados."""
import asyncio
import json
import threading
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.presentation import BREADCRUMB_HEADER, present_answer
from agent_core.providers import Provider
from agent_core.service import build_context
from agent_core.tools import SCHEMAS


class BreadcrumbProfileTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode = "tools"

    async def upstream(self, request):
        if request.url.path == "/api/chat" and json.loads(request.content).get("format"):
            self.requests.append((request.url.path, json.loads(request.content), dict(request.headers)))
            answer = "[Breadcrumb]\n<thinking>razonamiento de prueba</thinking>\n[Breadcrumb]\nHola, Baggute."
            return httpx.Response(200, json={"message": {"content": json.dumps({"answer": answer})}})
        return await fixtures.ProjectTest.upstream(self, request)

    def test_profile_loads_full_prompt_and_structured_request_fits(self):
        self.register()
        response = self.client.get("/api/agent/config")
        self.assertEqual(response.status_code, 200, response.text)
        listed = next(item for item in response.json()["profiles"] if item["id"] == "breadcrumb")
        self.assertEqual(listed["display_name"], "Breadcrumb")
        files = self.app.state.files
        profile = files.profile("breadcrumb")
        self.assertEqual(profile.tools, files.profile("equipo").tools)
        prompt = files.prompt(profile)
        self.assertTrue(prompt.endswith((self.root / "prompts/breadcrumb.md").read_text("utf-8")))
        self.assertIn("web_read", prompt)
        self.assertIn("project=true", prompt)
        backend = files.agent().backends["local"]
        messages = build_context(prompt, [], "hola", [], [SCHEMAS["task_plan"]], backend.context_window, backend.max_output_tokens)
        body, _ = Provider(backend, files.agent().limits).structured_request(messages, [SCHEMAS["task_plan"]], allow_answer=True)
        self.assertLess(len(json.dumps(body, ensure_ascii=False).encode("utf-8")), backend.context_window - backend.max_output_tokens - 256)

    def test_greeting_preserves_json_protocol_and_saves_one_visible_answer(self):
        headers = self.register()
        conversation = self.conversation(headers, "breadcrumb")
        identifier = str(uuid.uuid4())
        response = self.send(headers, conversation, "hola", identifier)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["answer"], "[Breadcrumb]\n\nHola, Baggute.")
        self.assertEqual(result["tools_used"], [])
        self.assertTrue(all(body.get("format") for path, body, _ in self.requests if path == "/api/chat"))
        self.assertEqual(self.send(headers, conversation, "hola", identifier).json(), result)
        history = self.client.get(f"/api/agent/conversations/{conversation}/messages").json()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["content"], result["answer"])

    def test_background_research_returns_pending_then_keeps_sources_and_status(self):
        headers = self.register()
        config = self.config()
        config["multi_agent"]["enabled"] = True
        self.save_config(config)
        conversation = self.conversation(headers, "breadcrumb")
        identifier = str(uuid.uuid4())
        release, entered = asyncio.Event(), threading.Event()
        sources = [{"id": 1, "kind": "web", "url": "https://example.org/fuente", "title": "Fuente simulada",
                    "excerpt": "Evidencia simulada para la prueba.", "retrieval": "page_extract"}]

        async def research(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"answer": "Evidencia disponible [1]", "sources": sources, "model_calls": 4,
                    "tools_used": ["web_search", "web_read"], "review": "team_partial", "team": {"status": "partial"}}

        with patch("agent_core.team_research.TeamResearch.run", AsyncMock(side_effect=research)):
            response = self.client.post(f"/api/agent/conversations/{conversation}/messages", headers=headers,
                json={"message": "Investiga documentación oficial con fuentes", "request_id": identifier, "background": True})
            self.assertIn(response.status_code, (200, 202), response.text)
            try:
                self.assertTrue(entered.wait(1))
                self.assertIn(response.json()["status"], {"queued", "running"})
                self.assertFalse(self.requests)
            finally:
                self.client.portal.call(release.set)
            route = f"/api/agent/conversations/{conversation}/jobs/{identifier}"
            for _ in range(200):
                job = self.client.get(route).json()
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(.01)
            self.assertEqual(job["status"], "complete", job)
            self.assertEqual(job["result"]["sources"], sources)
            self.assertEqual(job["result"]["answer"], BREADCRUMB_HEADER + "\n\nEvidencia disponible [1]")
            self.assertEqual(job["result"]["team"]["status"], "partial")
            self.assertEqual(job["result"]["review"], "team_partial")

    def test_download_keeps_pending_state_and_does_not_claim_completion(self):
        headers = self.register()
        conversation = self.conversation(headers, "breadcrumb")
        queued = {"status": "queued", "job": {"id": str(uuid.uuid4()), "status": "queued"}}
        with patch.object(self.app.state.download_jobs, "submit", AsyncMock(return_value=queued)):
            response = self.send(headers, conversation, "Descarga el PDF de https://example.org/paper")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["answer"].startswith(BREADCRUMB_HEADER + "\n"))
        self.assertIn("Continúa en segundo plano", result["answer"])
        self.assertEqual(result["download_job"]["status"], "queued")
        self.assertEqual(result["review"], "pending_download")
        self.assertEqual(result["model_calls"], 0)

    def test_presentation_keeps_literal_code_and_citations(self):
        profile = self.app.state.files.profile("breadcrumb")
        body = "Resultado [1]\n```html\n<thinking>etiqueta literal</thinking>\n```"
        for kind in ("coding", "math", "research", "download"):
            self.assertEqual(present_answer(profile, "tarea", body, kind), BREADCRUMB_HEADER + "\n\n" + body)
        self.assertNotIn("privado", present_answer(profile, "hola", "<think>privado"))


if __name__ == "__main__":
    unittest.main()
