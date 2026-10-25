"""Execution team: real HTTP/SQLite, simulated model and Docker observations."""
import json
import shutil
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.projects import ProjectFiles, describe
from agent_core.providers import Provider
from agent_core.team_control import SkillRegistry, TeamCheckpoints, TeamCouncil


class TeamExecutionTest(unittest.TestCase):
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    tearDown = fixtures.ProjectTest.tearDown

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        shutil.copytree(fixtures.PROJECT / "agent_skills", self.root / "agent_skills", dirs_exist_ok=True)
        config = self.config()
        config["multi_agent"].update(enabled=True, max_parallel_model_calls=1, max_repairs=0)
        self.save_config(config)
        self.mode = "tools"
        self.premature_answer = False
        self.omit_project_once = False
        self.docker_fails = False
        self.mutate_during_review = None
        self.action_requests, self.council_requests = [], []
        self.files = [{"path": "promedio.py", "content": "def promedio(valores):\n    if not valores:\n        raise ValueError('lista vacía')\n    return sum(valores) / len(valores)\n"},
                      {"path": "test_promedio.py", "content": "import unittest\nfrom promedio import promedio\n\nclass Prueba(unittest.TestCase):\n    def test_promedio(self):\n        self.assertEqual(promedio([2, 4]), 3)\n        with self.assertRaises(ValueError):\n            promedio([])\n\nif __name__ == '__main__':\n    unittest.main()\n"}]
        self.runner = SimpleNamespace(run=AsyncMock(side_effect=self.execute))
        patcher = patch("agent_core.tools.SandboxRunner", return_value=self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def execute(self, language, code, *, files):
        self.assertEqual(language, "python")
        self.assertEqual(describe(files), describe(self.files))
        self.assertTrue("discover" in code or "test_promedio" in code)
        if self.docker_fails:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "AssertionError: expected average"}
        return {"ok": True, "exit_code": 0, "stdout": "Ran 1 test\nOK\n", "stderr": ""}

    async def upstream(self, request):
        if request.url.path != "/api/chat":
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        schema = body.get("format", {})
        properties = schema.get("properties", {})
        if "requirements" in properties:
            self.council_requests.append(body)
            response = {"requirements": ["Guardar promedio.py y sus pruebas"],
                        "checks": ["Comprobar lista vacía y promedio de [2,4]"], "warnings": []}
        elif "status" in properties:
            self.council_requests.append(body)
            if self.mutate_during_review:
                conversation = self.mutate_during_review
                self.mutate_during_review = None
                self.project(conversation).write([{"path": "promedio.py", "content": "def promedio(valores):\n    return 999\n"}])
            response = {"status": "satisfied", "issues": []}
        else:
            self.action_requests.append(body)
            if self.premature_answer:
                response = {"answer": "Terminé y probé todo"}
            elif "files" in properties:
                response = {"files": self.files}
            elif "language" in properties:
                response = self.script()
            elif "anyOf" in schema:
                options = schema["anyOf"]
                allowed = [option.get("properties", {}).get("tool", {}).get("enum", []) for option in options]
                if any("sandbox_exec" in tools for tools in allowed):
                    response = {"tool": "sandbox_exec", "arguments": self.script()}
                else:
                    response = {"answer": "Los archivos solicitados se guardaron y la ejecución observada terminó con Ran 1 test y OK."}
            else:
                self.fail("Unexpected model contract: " + json.dumps(schema))
        return httpx.Response(200, json={"message": {"content": json.dumps(response, ensure_ascii=False)}})

    def script(self):
        project = not self.omit_project_once
        self.omit_project_once = False
        return {"language": "python", "code": "import unittest\nimport test_promedio\nsuite = unittest.defaultTestLoader.loadTestsFromModule(test_promedio)\nresult = unittest.TextTestRunner().run(suite)\nassert result.wasSuccessful()\n", "purpose": "Comprobar la versión guardada del promedio", "project": project}

    def prompt(self):
        return "Crea promedio.py y test_promedio.py con una función promedio que lance ValueError para [] y dé 3 para [2,4]. Guarda y ejecuta las pruebas del proyecto en Docker."

    def project(self, conversation, user_id=1):
        return ProjectFiles(self.app.state.database, user_id, conversation)

    def activity(self, conversation, request_id):
        response = self.client.get(f"/api/agent/conversations/{conversation}/activity", params={"request_id": request_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_controller_requires_saved_project_execution_and_final_manifest(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        request_id = str(uuid.uuid4())
        response = self.send(headers, conversation, self.prompt(), request_id)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["tools_used"], ["task_plan", "project_write", "sandbox_exec"])
        self.assertEqual(result["team"]["status"], "complete")
        self.assertEqual(result["team"]["review"]["status"], "satisfied")
        self.assertEqual(result["review"], "model_reviewed")
        self.assertFalse(result["project_unverified"])
        self.assertTrue(result["task_graph"]["complete"])
        saved_files = self.project(conversation).read()
        self.assertEqual(saved_files, self.files)
        self.assertEqual(result["tested_project"], describe(saved_files))
        self.assertEqual(self.project(conversation).checked(), describe(saved_files))
        self.runner.run.assert_awaited_once()
        self.assertEqual(self.runner.run.await_args.kwargs["files"], saved_files)
        self.assertEqual(self.action_requests[0]["format"]["required"], ["files"])
        self.assertIn("discover", self.runner.run.await_args.args[1])
        self.assertIn("countTestCases", self.runner.run.await_args.args[1])
        self.assertIn("count == 0", self.runner.run.await_args.args[1])
        self.assertNotIn("def promedio", self.runner.run.await_args.args[1])
        self.assertEqual(len(self.council_requests), 3)
        events = self.activity(conversation, request_id)["events"]
        self.assertTrue(any(event["kind"] == "team_workflow" for event in events))
        self.assertTrue(any(event["kind"] == "team_verification" for event in events))
        artifacts = [event["payload"] for event in events if event["kind"] == "team_artifact"]
        self.assertEqual([artifact["checked"] for artifact in artifacts], [False, True])
        records = self.client.get(f"/api/agent/conversations/{conversation}/team-artifacts/{request_id}")
        self.assertEqual(records.status_code, 200)
        self.assertIn("project:", records.text)
        self.assertIn("promedio.py", records.text)

    def test_premature_model_answer_cannot_replace_required_files_or_publish_success(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        self.premature_answer = True
        response = self.send(headers, conversation, self.prompt())
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["code"], "invalid_structured_action")
        self.assertEqual(self.project(conversation).read(), [])
        self.assertEqual(self.project(conversation).checked(), [])
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/messages").json(), [])
        self.runner.run.assert_not_awaited()

    def test_project_false_is_rejected_before_runner_and_corrected_execution_can_finish(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        self.omit_project_once = True
        request_id = str(uuid.uuid4())
        response = self.send(headers, conversation, "Crea promedio.py y ejecuta los archivos guardados del proyecto en Docker.", request_id)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["team"]["status"], "complete")
        self.runner.run.assert_awaited_once()
        self.assertEqual(result["tested_project"], describe(self.project(conversation).read()))
        events = self.activity(conversation, request_id)["events"]
        failures = [event for event in events if event["kind"] == "observation" and event["payload"]["result"].get("error") == "project_test_required"]
        self.assertEqual(len(failures), 1)

    def test_repeated_docker_failure_does_not_claim_a_verified_or_completed_result(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        self.docker_fails = True
        request_id = str(uuid.uuid4())
        response = self.send(headers, conversation, self.prompt(), request_id)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['review'], 'unresolved')
        self.assertEqual(result['team']['status'], 'partial')
        self.assertFalse(result['task_graph']['complete'])
        self.assertTrue(result['project_unverified'])
        self.assertEqual(result['tested_project'], [])
        self.assertIn('La tarea quedó pendiente', result['answer'])
        self.assertIn('Pasos pendientes: comprobar', result['answer'])
        self.assertGreater(self.runner.run.await_count, 0)
        self.assertEqual(self.project(conversation).checked(), [])
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conversation}/messages").json()), 2)
        events = self.activity(conversation, request_id)["events"]
        self.assertTrue(any(event['kind'] == 'budget_handoff' for event in events))
        self.assertTrue(any(event["kind"] == "observation" and event["payload"]["result"].get("exit_code") == 1 for event in events))
        self.assertFalse(any(event["kind"] == "team_completed" and event["payload"].get("status") == "complete" for event in events))

    def test_positive_model_review_with_no_files_is_overridden_by_evidence_guard(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        config, backend = self.app.state.agent.backend("local")
        skill = SkillRegistry(self.root).load("coding")
        checkpoints = TeamCheckpoints(self.app.state.database, 1, conversation, "empty-evidence", self.prompt(), skill)
        events = []
        council = TeamCouncil(config.multi_agent, Provider(backend, config.limits, self.transport), skill,
                              lambda *event: events.append(event), checkpoints)
        tools = SimpleNamespace(project=self.project(conversation), project_dirty=False,
                                tested_project=[], pending_failures=[], attempts={})
        report = self.client.portal.call(council.review, self.prompt(), "Todo guardado y probado", tools)
        self.assertEqual(report["status"], "needs_correction")
        self.assertIn("No hay archivos", report["issues"][0])
        self.assertEqual(self.project(conversation).read(), [])
        self.assertFalse(any(event[0] == "team_completed" for event in events))

    def test_project_changed_while_reviewer_waits_cannot_reuse_old_checked_manifest(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        self.mutate_during_review = conversation
        response = self.send(headers, conversation, self.prompt())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["team"]["status"], "partial")
        self.assertEqual(result["review"], "unresolved")
        self.assertEqual(result["team"]["review"]["status"], "needs_correction")
        self.assertTrue(result["project_unverified"])
        self.assertEqual(self.project(conversation).checked(), [])
        self.assertNotEqual(result["tested_project"], describe(self.project(conversation).read()))

    def test_private_project_versions_and_team_reports_cannot_be_read_by_other_owner(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        request_id = str(uuid.uuid4())
        response = self.send(headers, conversation, self.prompt(), request_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.client.cookies.clear()
        other_headers = self.register("foreign-owner")
        for path in (f"/api/agent/conversations/{conversation}/project", f"/api/agent/conversations/{conversation}/project.zip",
                     f"/api/agent/conversations/{conversation}/messages", f"/api/agent/conversations/{conversation}/team-artifacts/{request_id}"):
            denied = self.client.get(path)
            self.assertEqual(denied.status_code, 404, path)
            self.assertNotIn("test_promedio.py", denied.text)
        self.assertEqual(self.send(other_headers, conversation, self.prompt()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
