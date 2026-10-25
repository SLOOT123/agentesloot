"""SQLite, rutas y ZIP reales; decisiones del modelo y Docker simulados."""
import asyncio
import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.config import AgentError
from agent_core.projects import ProjectFiles, validate_files
from agent_core.storage import Database
from agent_core.tools import Tools


class ProjectsTest(unittest.TestCase):
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    tearDown = fixtures.ProjectTest.tearDown

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode, self.model_step, self.repair = "tools", 0, False
        self.files = [{"path": "src/promedio.py", "content": "def promedio(v):\n    return sum(v)/len(v) if v else None\n"},
                      {"path": "README.md", "content": "Proyecto privado de prueba"}]
        self.runner = SimpleNamespace(run=AsyncMock(side_effect=self.execute))
        patcher = patch("agent_core.tools.SandboxRunner", return_value=self.runner)
        patcher.start(); self.addCleanup(patcher.stop)

    async def execute(self, language, code, *, files):
        self.assertEqual(language, "python")
        self.assertEqual(files[0]["path"], "README.md")
        if self.repair and self.runner.run.await_count == 1:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "ZeroDivisionError"}
        return {"ok": True, "exit_code": 0, "stdout": "pruebas_ok", "stderr": ""}

    async def upstream(self, request):
        if request.url.path != "/api/chat":
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        self.model_step += 1
        name, args = None, None
        if self.model_step == 1:
            files = [{"path": item["path"], "content": item["content"]} for item in self.files]
            if self.repair:
                files[0]["content"] = "def promedio(v):\n    return sum(v)/len(v)\n"
            name, args = "project_write", {"files": files}
        elif self.model_step == 2 or (self.repair and self.model_step == 4):
            name, args = "sandbox_exec", {"language": "python", "project": True,
                "code": "from src.promedio import promedio\nassert promedio([]) is None\nassert promedio([2,4]) == 3\nprint('pruebas_ok')", "purpose": "Verificar el proyecto promedio"}
            if self.model_step == 4:
                previous = next(json.loads(m["content"]) for m in body["messages"] if m.get("tool_name") == "sandbox_exec")
                args["repair_of"] = previous["attempt_id"]
        elif self.repair and self.model_step == 3:
            name, args = "project_write", {"files": [self.files[0]]}
        message = {"content": "Proyecto guardado y pruebas simuladas terminadas; no está publicado."}
        if name:
            message = {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
        return httpx.Response(200, json={"message": message})

    def project(self, conv, user_id=1):
        return ProjectFiles(self.app.state.database, user_id, conv)

    def test_model_creates_project_tests_snapshot_and_downloads_real_zip(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        response = self.send(headers, conv, "Crea un proyecto pequeño para calcular promedios")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tools_used"], ["project_write", "sandbox_exec"])
        self.assertEqual(response.json()["review"], "model_reviewed")
        self.assertEqual(self.runner.run.await_count, 1)
        stored = self.client.get(f"/api/agent/conversations/{conv}/project").json()["files"]
        self.assertEqual({item["path"] for item in stored}, {"src/promedio.py", "README.md"})
        download = self.client.get(f"/api/agent/conversations/{conv}/project.zip")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["content-type"], "application/zip")
        self.assertIn("attachment", download.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertEqual(archive.read("README.md").decode(), "Proyecto privado de prueba")
            self.assertIn("def promedio", archive.read("src/promedio.py").decode())
        activity = self.client.get(f"/api/agent/conversations/{conv}/activity").json()
        script = next(e for e in activity["events"] if e["kind"] == "script")
        self.assertEqual(len(script["payload"]["project_files"]), 2)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in script["payload"]["project_files"]))
        restarted = Database(self.settings.db_path)
        restarted.initialize()
        self.assertEqual(len(ProjectFiles(restarted, 1, conv).read()), 2)

    def test_project_correction_fits_existing_budget_and_preserves_association(self):
        self.repair = True
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        response = self.send(headers, conv, "Verifica el proyecto promedio")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["model_calls"], 6)
        self.assertEqual(len(response.json()["lessons_saved"]), 1)
        self.assertEqual(self.runner.run.await_count, 2)
        first, last = self.runner.run.await_args_list
        self.assertNotEqual(first.kwargs["files"][1]["content"], last.kwargs["files"][1]["content"])

    def test_projects_are_private_between_conversations_and_users(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        self.project(conv).write(self.files)
        other = self.conversation(headers, "laboratorio")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{other}/project").json()["files"], [])
        self.assertEqual(self.client.get(f"/api/agent/conversations/{other}/project.zip").status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/project.zip").status_code, 401)
        self.register("otro")
        for suffix in ("project", "project.zip"):
            self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/{suffix}").status_code, 404)
        with self.assertRaises(AgentError):
            self.project(conv, user_id=2).write(self.files)

    def test_invalid_batch_is_atomic_and_limits_apply_to_combined_project(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        store = self.project(conv)
        store.write(self.files)
        with self.assertRaises(AgentError):
            store.write([{"path": "README.md", "content": "cambio"}, {"path": "../escape", "content": "x"}])
        self.assertEqual(store.read()[0]["content"], "Proyecto privado de prueba")
        with self.assertRaises(AgentError):
            store.write([{"path": "src", "content": "colisión carpeta"}])
        with self.assertRaises(AgentError):
            store.write([{"path": f"archivo{i}.txt", "content": "x"} for i in range(32)])
        with self.assertRaises(AgentError):
            store.write([{"path": f"grande{i}.txt", "content": "x" * 16000} for i in range(8)])
        self.assertEqual(len(store.read()), 2)

    def test_read_is_paginated_and_empty_project_never_executes(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        tools = Tools(self.app.state.database, 1, 5, conversation_id=conv, runner=self.runner)

        async def check():
            with self.assertRaises(AgentError):
                await tools.script({"language": "python", "code": "print(1)", "purpose": "Vacío", "project": True})
            self.runner.run.assert_not_awaited()
            self.project(conv).write([{"path": "largo.txt", "content": "a" * 1500}])
            first = await tools.execute("project_read", {"path": "largo.txt"}, ["project_read"])
            second = await tools.execute("project_read", {"path": "largo.txt", "offset": 1000}, ["project_read"])
            self.assertEqual(first["file"]["next_offset"], 1000)
            self.assertEqual(len(first["file"]["content"]), 1000)
            self.assertEqual(len(second["file"]["content"]), 500)
            self.assertIsNone(second["file"]["next_offset"])
        asyncio.run(check())


class ProjectValidationTest(unittest.TestCase):
    def test_media_names_cannot_turn_generated_text_into_fake_downloads(self):
        for path in ("cumbia.mp3", "audio.WAV", "clip.mp4", "imagen.png"):
            with self.subTest(path=path), self.assertRaises(AgentError) as caught:
                validate_files([{"path": path, "content": "Esto es texto, no audio"}])
            self.assertEqual(caught.exception.code, "text_files_only")
        self.assertEqual(validate_files([{"path": "audio.svg", "content": "<svg/>"}])[0]["path"], "audio.svg")

    def test_portable_paths_sizes_and_no_traversal(self):
        for path in ("../x", "/tmp/x", "C:/x", "a\\b", "a/../x", "a//b", "NUL.txt", "x/CON", "a.", ".", "a\x00b"):
            with self.subTest(path=path), self.assertRaises(AgentError):
                validate_files([{"path": path, "content": "x"}])
        for files in ([{"path": "A.py", "content": ""}, {"path": "a.py", "content": ""}],
                      [{"path": "a", "content": ""}, {"path": "a/b", "content": ""}],
                      [{"path": "a", "content": "á" * 8001}], [{"path": "a", "content": "\x00"}]):
            with self.assertRaises(AgentError):
                validate_files(files)
