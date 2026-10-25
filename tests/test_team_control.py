"""Contratos de skills, evidencias privadas y controles del equipo sin red."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from agent_core.config import AgentError
from agent_core.storage import Database
from agent_core.team_control import SkillRegistry, TeamCheckpoints, TeamCouncil, execution_skill


PROJECT = Path(__file__).resolve().parent.parent


class TeamControlTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / "agent_skills", self.root / "agent_skills")
        self.database = Database(self.root / "test.sqlite3")
        self.database.initialize()
        with self.database.connection() as connection:
            self.user = connection.execute("INSERT INTO users(username,password) VALUES('owner','unused')").lastrowid
            self.other = connection.execute("INSERT INTO users(username,password) VALUES('foreign','unused')").lastrowid
        self.conversation = self.database.create_conversation(self.user, "agente", "local")["id"]
        self.foreign = self.database.create_conversation(self.other, "agente", "local")["id"]
        self.registry = SkillRegistry(self.root)
        self.skill = self.registry.load("coding")
        self.checkpoint = self.make_checkpoint()
        self.events = []
        self.config = SimpleNamespace(max_parallel_model_calls=1)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def make_checkpoint(self, request_id="request", message="Crear programa", skill=None, conversation=None, user=None):
        return TeamCheckpoints(self.database, self.user if user is None else user, conversation or self.conversation,
                               request_id, message, self.skill if skill is None else skill)

    def event(self, kind, title, payload):
        self.events.append((kind, title, payload))

    async def test_project_local_skills_have_strict_ids_and_contracts(self):
        for name in ("research", "coding", "math", "download"):
            skill = self.registry.load(name)
            self.assertEqual(skill["id"], name)
            self.assertRegex(skill["version"], r"^\d+\.\d+\.\d+$")
            self.assertTrue(skill["acceptance_criteria"])
            self.assertLessEqual(len(skill["acceptance_criteria"]), 8)
        for name in ("../coding", "C:/temp/coding", "unknown", "coding.json"):
            with self.assertRaises(AgentError) as error:
                self.registry.load(name)
            self.assertEqual(error.exception.code, "unknown_skill")
        path = self.root / "agent_skills" / "coding.json"
        for changed in ({**self.skill, "id": "math"}, {**self.skill, "acceptance_criteria": ["x"] * 9},
                        {**self.skill, "acceptance_criteria": ["x" * 301]}, {**self.skill, "extra": "not allowed"}):
            path.write_text(json.dumps(changed), "utf-8")
            with self.assertRaises(AgentError) as error:
                self.registry.load("coding")
            self.assertEqual(error.exception.code, "invalid_skill")

    async def test_checkpoint_identity_separates_owner_conversation_request_prompt_and_skill(self):
        payload = {"evidence": "Sólo para este trabajo"}
        self.checkpoint("worker:one", payload)
        self.assertEqual(self.make_checkpoint().load("worker:one"), payload)
        for checkpoint in (self.make_checkpoint(request_id="other"), self.make_checkpoint(message="Cambió la petición"),
                           self.make_checkpoint(skill={**self.skill, "version": "99.0.0"}),
                           self.make_checkpoint(conversation=self.foreign, user=self.other)):
            self.assertIsNone(checkpoint.load("worker:one"))
        with self.assertRaises(AgentError) as denied:
            self.make_checkpoint(conversation=self.foreign)
        self.assertEqual(denied.exception.status, 404)
        self.checkpoint("worker:one", {"evidence": "Actualizada"})
        self.assertEqual(self.checkpoint.load("worker:one"), {"evidence": "Actualizada"})
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_team_checkpoints").fetchone()[0], 1)

    async def test_project_snapshot_hash_is_deterministic_and_keeps_private_files(self):
        files = [{"path": "main.py", "content": "print(4)\n"}]
        expected = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        digest = self.checkpoint.snapshot(files, checked=True)
        self.assertEqual(digest, expected)
        self.assertEqual(self.checkpoint.load("project:" + digest), {"files": files, "sha256": digest, "checked": True})
        self.assertIsNone(self.make_checkpoint(conversation=self.foreign, user=self.other).load("project:" + digest))
        changed = self.checkpoint.snapshot([{**files[0], "content": "print(5)\n"}], checked=False)
        self.assertNotEqual(changed, digest)
        self.assertTrue(self.checkpoint.load("project:" + digest)["checked"])
        self.assertFalse(self.checkpoint.load("project:" + changed)["checked"])
        with self.assertRaises(AgentError) as too_large:
            self.checkpoint("big", {"data": "x" * (1024 * 1024)})
        self.assertEqual(too_large.exception.code, "team_artifact_limit")

    async def test_council_rejects_non_object_json_and_invalid_reports_are_uncertain(self):
        provider = SimpleNamespace(complete_json=AsyncMock())
        council = TeamCouncil(self.config, provider, self.skill, self.event, self.checkpoint)
        for invalid in ("no JSON", "[]", "null", "123"):
            provider.complete_json.return_value = SimpleNamespace(content=invalid)
            with self.assertRaises(AgentError) as error:
                await council.json_call("test", "Revisa", {"type": "object"})
            self.assertEqual(error.exception.code, "team_invalid_response")
        provider.complete_json.return_value = SimpleNamespace(content=json.dumps({"requirements": ["x" * 221], "checks": [], "warnings": []}))
        guidance = await council.prepare("Crear programa")
        self.assertIn("no produjo un informe válido", guidance)
        for role in ("requisitos", "verificacion"):
            self.assertEqual(self.checkpoint.load("council:" + role)["checks"], [])
        project = SimpleNamespace(read=lambda: [])
        tools = SimpleNamespace(project=project, project_dirty=False, tested_project=[], attempts={}, pending_failures=[])
        provider.complete_json.return_value = SimpleNamespace(content=json.dumps({"status": "satisfied", "issues": ["x"] * 5}))
        report = await council.review("Crear programa", "Supuesto resultado", tools)
        self.assertEqual(report["status"], "uncertain")

    async def test_specialist_model_concurrency_is_bounded_and_cached_reports_skip_provider(self):
        active, peak = 0, 0
        async def complete_json(messages, schema, output_limit):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                self.assertEqual(output_limit, 512)
                self.assertIn("No tienes herramientas", messages[0]["content"])
                await asyncio.sleep(.01)
                return SimpleNamespace(content=json.dumps({"requirements": ["Archivo solicitado"], "checks": ["Prueba independiente"], "warnings": []}))
            finally:
                active -= 1
        provider = SimpleNamespace(complete_json=AsyncMock(side_effect=complete_json))
        council = TeamCouncil(self.config, provider, self.skill, self.event, self.checkpoint)
        await council.prepare("Crear programa")
        self.assertEqual(provider.complete_json.await_count, 2)
        self.assertEqual(peak, 1)
        restored = TeamCouncil(self.config, provider, self.skill, self.event, self.make_checkpoint())
        await restored.prepare("Crear programa")
        self.assertEqual(provider.complete_json.await_count, 2)
        self.assertTrue(any(item[2].get("cached") for item in self.events))

    async def test_reviewer_status_with_wrong_json_type_is_uncertain(self):
        tools = SimpleNamespace(project=SimpleNamespace(read=lambda: []), project_dirty=False,
                                tested_project=[], attempts={}, pending_failures=[])
        for index, invalid_status in enumerate(([], {})):
            provider = SimpleNamespace(complete_json=AsyncMock(return_value=SimpleNamespace(
                content=json.dumps({"status": invalid_status, "issues": []}))))
            council = TeamCouncil(self.config, provider, self.skill, self.event, self.make_checkpoint(request_id="invalid-" + str(index)))
            report = await council.review("Crear programa", "Borrador", tools)
            self.assertEqual(report["status"], "uncertain")

    async def test_specialist_context_limit_blocks_the_provider_before_dispatch(self):
        provider = SimpleNamespace(backend=SimpleNamespace(context_window=2048), complete_json=AsyncMock())
        council = TeamCouncil(self.config, provider, self.skill, self.event, self.checkpoint)
        with self.assertRaises(AgentError) as error:
            await council.json_call("revisor", "ñ" * 1500, {"type": "object"})
        self.assertEqual(error.exception.code, "team_context_limit")
        provider.complete_json.assert_not_awaited()
        self.assertEqual(council.model_calls, 0)

    async def test_unverified_execution_overrides_a_positive_model_review(self):
        provider = SimpleNamespace(complete_json=AsyncMock(return_value=SimpleNamespace(content=json.dumps({"status": "satisfied", "issues": []}))))
        council = TeamCouncil(self.config, provider, self.skill, self.event, self.checkpoint)
        tools = SimpleNamespace(project=SimpleNamespace(read=lambda: [{"path": "main.py", "content": "print(4)"}]),
                                project_dirty=True, tested_project=[], attempts={}, pending_failures=[])
        report = await council.review("Crear programa", "Terminado", tools)
        self.assertEqual(report["status"], "needs_correction")
        self.assertIn("sin comprobar", report["issues"][0])
        tools.project_dirty = False
        tools.pending_failures = ["failure"]
        report = await council.review("Crear programa", "Terminado", tools, attempt=1)
        self.assertEqual(report["status"], "needs_correction")

    async def test_cached_positive_review_cannot_hide_new_unverified_state(self):
        provider = SimpleNamespace(complete_json=AsyncMock(return_value=SimpleNamespace(
            content=json.dumps({"status": "satisfied", "issues": []}))))
        council = TeamCouncil(self.config, provider, self.skill, self.event, self.checkpoint)
        tools = SimpleNamespace(project=SimpleNamespace(read=lambda: [{"path": "main.py", "content": "print(4)"}]),
                                project_dirty=False, tested_project=[{"path": "main.py"}], attempts={}, pending_failures=[])
        original = await council.review("Crear programa", "Terminado", tools)
        self.assertEqual(original["status"], "satisfied")
        # Mismos archivos, borrador e intento: cambia la evidencia técnica.
        tools.pending_failures = ["new-failed-check"]
        after_failure = await council.review("Crear programa", "Terminado", tools)
        self.assertEqual(after_failure["status"], "needs_correction")
        tools.pending_failures = []
        tools.project_dirty = True
        after_dirty = await council.review("Crear programa", "Terminado", tools)
        self.assertEqual(after_dirty["status"], "needs_correction")

    async def test_execution_skills_route_code_and_math_without_capturing_a_greeting(self):
        self.assertEqual(execution_skill("Crea un programa Python y comprueba sus pruebas"), "coding")
        self.assertEqual(execution_skill("Resuelve esta ecuación matemática"), "math")
        self.assertIsNone(execution_skill("hola"))


if __name__ == "__main__":
    unittest.main()
