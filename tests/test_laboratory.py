"""Integración real de rutas/SQLite; modelo y ejecutor simulados. Docker real se prueba aparte."""
import json
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.config import SandboxConfig
from agent_core.relations import research_graph
from agent_core.sandbox import SandboxRunner


class LaboratoryTest(unittest.TestCase):
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    tearDown = fixtures.ProjectTest.tearDown

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.scenario, self.model_step = "repair", 0
        self.runner = SimpleNamespace(run=AsyncMock(side_effect=[
            {"ok": False, "exit_code": 1, "stderr": "ZeroDivisionError: division by zero", "stdout": ""},
            {"ok": True, "exit_code": 0, "stderr": "", "stdout": "2\n"}]))
        patcher = patch("agent_core.tools.SandboxRunner", return_value=self.runner)
        patcher.start(); self.addCleanup(patcher.stop)

    async def upstream(self, request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"] + ([] if self.scenario == "no_tools" else ["tools"]), "model_info": {"test.context_length": 32768}})
        if request.url.path != "/api/chat":
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        self.model_step += 1
        if self.scenario == "text":
            message = {"content": "Respuesta revisada simulada con la memoria pertinente."}
        elif self.scenario == "graph":
            if self.model_step == 1:
                message = {"content": "", "tool_calls": [{"function": {"name": "web_search", "arguments": {"query": "relaciones de prueba"}}}]}
            elif self.model_step == 2:
                message = {"content": "", "tool_calls": [{"function": {"name": "research_graph", "arguments": {}}}]}
            else:
                message = {"content": "El grafo muestra relaciones textuales; no se comprobó causalidad."}
        elif self.model_step == 1:
            message = {"content": "", "tool_calls": [{"function": {"name": "sandbox_exec", "arguments": {"language": "python", "code": "print(1 / 0)", "purpose": "Calcular promedio de la prueba"}}}]}
        elif self.model_step == 2:
            previous = json.loads(next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool"))
            args = {"language": "python", "code": "values = [1,2,3]\nassert sum(values)/len(values) == 2\nprint(2)", "purpose": "Calcular promedio de la prueba"}
            if self.scenario != "unrelated":
                args["repair_of"] = previous["attempt_id"]
            message = {"content": "", "tool_calls": [{"function": {"name": "sandbox_exec", "arguments": args}}]}
        else:
            message = {"content": "Promedio 2. Se ejecutó una aserción para el ejemplo; no valida todos los casos."}
        return httpx.Response(200, json={"message": message})

    def test_error_repair_review_and_observed_lesson(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        response = self.send(headers, conv, "Comprueba el promedio de la prueba")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review"], "model_reviewed")
        self.assertEqual(response.json()["model_calls"], 4)
        self.assertEqual(len(response.json()["lessons_saved"]), 1)
        lessons = self.client.get("/api/agent/lessons").json()
        self.assertEqual(lessons[0]["status"], "observed")
        self.assertIn("ZeroDivisionError", lessons[0]["error"])
        activity = self.client.get(f"/api/agent/conversations/{conv}/activity").json()
        self.assertEqual(activity["status"], "complete")
        kinds = [event["kind"] for event in activity["events"]]
        self.assertEqual(kinds.count("script"), 2)
        self.assertIn("review", kinds)
        self.assertEqual(kinds[-1], "completed")
        script_events = [event for event in activity["events"] if event["kind"] == "script"]
        self.assertEqual(script_events[1]["payload"]["repair_of"], script_events[0]["payload"]["attempt_id"])
        self.assertEqual(self.runner.run.await_count, 2)

    def test_unrelated_success_does_not_erase_failure_or_become_a_lesson(self):
        self.scenario = "unrelated"
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        response = self.send(headers, conv)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review"], "unresolved")
        self.assertIn("no está verificada", response.json()["answer"])
        self.assertEqual(self.client.get("/api/agent/lessons").json(), [])

    def test_laboratory_requires_model_tools(self):
        self.scenario = "no_tools"
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        response = self.send(headers, conv)
        self.assertEqual(response.json()["code"], "laboratory_tools_required")
        self.runner.run.assert_not_awaited()

    def test_lessons_are_retrieved_and_rejected_lessons_are_excluded(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        result = self.send(headers, conv, "Calcula el promedio de prueba")
        self.assertEqual(result.status_code, 200, result.text)
        lesson = self.client.get("/api/agent/lessons").json()[0]
        self.scenario = "text"
        other = self.conversation(headers, "laboratorio")
        response = self.send(headers, other, "Consulta el promedio de prueba")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any(s.get("lesson_id") == lesson["id"] for s in response.json()["sources"]))
        self.assertIn("ZeroDivisionError", json.dumps(self.requests[-1][1]))
        response = self.client.post(f"/api/agent/lessons/{lesson['id']}/feedback", headers=headers, json={"verdict": "incorrect"})
        self.assertEqual(response.json()["status"], "rejected")
        newest = self.conversation(headers, "laboratorio")
        response = self.send(headers, newest, "Consulta el promedio de prueba")
        self.assertFalse(any(s.get("lesson_id") == lesson["id"] for s in response.json()["sources"]))
        self.assertIn("experiencia descartada por el usuario", json.dumps(self.requests[-1][1], ensure_ascii=False))

    def test_laboratory_reports_insufficient_context_before_execution(self):
        self.configure_model(context_window=8192)
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        self.assertEqual(self.send(headers, conv).json()["code"], "profile_context_limit")
        self.runner.run.assert_not_awaited()

    def test_missing_search_is_reported_as_unresolved_after_review(self):
        self.scenario = "graph"
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        with patch.dict("os.environ", {"SEARXNG_URL": ""}):
            response = self.send(headers, conv, "Relaciona las fuentes")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review"], "unresolved")
        self.assertEqual(response.json()["lessons_saved"], [])

    def test_activity_and_feedback_are_private_and_protected(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        self.assertEqual(self.send(headers, conv).status_code, 200)
        lesson_id = self.client.get("/api/agent/lessons").json()[0]["id"]
        self.assertEqual(self.client.post(f"/api/agent/lessons/{lesson_id}/feedback", json={"verdict": "helpful"}).status_code, 403)
        self.client.cookies.clear()
        bob = self.register("bob")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/activity").status_code, 404)
        self.assertEqual(self.client.get("/api/agent/lessons").json(), [])
        self.assertEqual(self.client.post(f"/api/agent/lessons/{lesson_id}/feedback", headers=bob, json={"verdict": "helpful"}).status_code, 404)

    def test_container_events_reach_private_activity_and_cursor_keeps_the_final_result(self):
        results = iter([
            {"ok": False, "exit_code": 1, "stdout": "", "stderr": "division by zero"},
            {"ok": True, "exit_code": 0, "stdout": "2\n", "stderr": ""}])
        async def run(*args, **kwargs):
            result = next(results)
            metadata = {"container": "community-lab-test-monitor", "purpose": "code", "network": "none"}
            self.runner.event("sandbox_started", "Contenedor solicitado", metadata)
            self.runner.event("sandbox_output", "Consola", {**metadata, "stdout": "SALIDA PRIVADA"})
            self.runner.event("sandbox_finished", "Contenedor finalizado", {**metadata, **result, "status": "complete" if result["ok"] else "failed"})
            return result
        self.runner.run.side_effect = run
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        self.assertEqual(self.send(headers, conv).status_code, 200)
        activity = self.client.get(f"/api/agent/conversations/{conv}/activity").json()
        output = next(e for e in activity["events"] if e["kind"] == "sandbox_output")
        self.assertIn("SALIDA PRIVADA", output["payload"]["stdout"])
        later = self.client.get(f"/api/agent/conversations/{conv}/activity", params={"request_id": activity["request_id"], "after": output["id"]}).json()
        self.assertTrue(all(e["id"] > output["id"] for e in later["events"]))
        self.assertTrue(any(e["kind"] == "sandbox_finished" and e["payload"]["status"] == "complete" for e in later["events"]))
        self.client.cookies.clear()
        self.register("other-monitor-user")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/activity").status_code, 404)

    def test_console_samples_leave_room_for_terminal_events(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        database = self.app.state.agent.database
        for index in range(80):
            database.event(conv, "monitor-output-budget", "sandbox_output", "Consola", {"stdout": str(index)})
        database.event(conv, "monitor-output-budget", "sandbox_finished", "Contenedor finalizado", {"status": "complete"})
        with database.connection() as connection:
            kinds = [row[0] for row in connection.execute("SELECT kind FROM agent_events WHERE conversation_id=? AND request_id=? ORDER BY id", (conv, "monitor-output-budget"))]
        self.assertEqual(kinds.count("sandbox_output"), 70)
        self.assertEqual(kinds[-1], "sandbox_finished")

    def test_search_and_graph_are_observable_in_same_run(self):
        self.scenario = "graph"
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        response = self.send(headers, conv, "Relaciona las fuentes de prueba")
        self.assertEqual(response.status_code, 200, response.text)
        activity = self.client.get(f"/api/agent/conversations/{conv}/activity").json()
        results = [e["payload"].get("result", {}) for e in activity["events"]]
        graph = next(result["graph"] for result in results if "graph" in result)
        self.assertEqual(graph["statistics"]["vertices"], 1)
        self.assertEqual(graph["statistics"]["beta_1"], 0)


class RelationsAndSandboxTest(unittest.TestCase):
    def test_runner_reports_streams_and_failure_with_the_same_container_identity(self):
        stdin = SimpleNamespace(write=lambda _: None, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock())
        process = SimpleNamespace(stdin=stdin, returncode=1,
            stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"primero\n", b"", b""])),
            stderr=SimpleNamespace(read=AsyncMock(side_effect=[b"error real\n", b"", b""])), wait=AsyncMock(return_value=1))
        runner = SandboxRunner(SandboxConfig(enabled=True))
        events = []
        runner.event = lambda kind, title, payload: events.append((kind, payload))
        with patch("agent_core.sandbox.shutil.which", return_value="docker"), patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            result = asyncio.run(runner.run("python", "print('primero')"))
        self.assertFalse(result["ok"])
        self.assertEqual(events[0][0], "sandbox_started")
        self.assertIn("sandbox_output", [kind for kind, _ in events])
        self.assertEqual(events[-1][0], "sandbox_finished")
        self.assertEqual(events[-1][1]["status"], "failed")
        self.assertEqual(events[-1][1]["stderr"], "error real\n")
        self.assertEqual(len({payload["container"] for _, payload in events}), 1)

    def test_repair_budget_stops_extra_executions(self):
        import asyncio
        from agent_core.tools import Tools
        runner = SimpleNamespace(run=AsyncMock(return_value={"ok": False, "exit_code": 1, "stderr": "Error"}))

        async def check():
            tools = Tools(None, 1, 10, max_repairs=0, runner=runner)
            first = await tools.script({"language": "python", "code": "1/0", "purpose": "Prueba"})
            retry = await tools.script({"language": "python", "code": "print(1)", "purpose": "Prueba", "repair_of": first["attempt_id"]})
            self.assertEqual(retry["error"], "repair_limit")
            self.assertEqual(runner.run.await_count, 1)
        asyncio.run(check())

    def test_graph_components_and_cycle_rank(self):
        sources = [{"id": index, "title": "Estudio datos relaciones", "excerpt": "matematica redes investigacion", "url": "https://example.org/" + str(index)} for index in range(1, 4)]
        graph = research_graph(sources)
        self.assertEqual(graph["statistics"]["beta_0"], 1)
        self.assertEqual(graph["statistics"]["beta_1"], 1)
        self.assertEqual(graph["statistics"]["density"], 1)
        self.assertEqual(research_graph([])["statistics"]["beta_0"], 0)
        isolated = research_graph([{"id": 1, "title": "Peras", "excerpt": "", "url": ""}, {"id": 2, "title": "Saturnos", "excerpt": "", "url": ""}])
        self.assertEqual(isolated["statistics"]["beta_0"], 2)
        self.assertEqual(isolated["statistics"]["beta_1"], 0)

    def test_runner_flags_and_no_host_environment_or_mount(self):
        runner = SandboxRunner(SandboxConfig(enabled=True))
        command = runner.command("docker", "community-lab-fixture", "python")
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pull=never", "--user=65534:65534"):
            self.assertIn(flag, command)
        for forbidden in ("--privileged", "--volume", "--mount", "--env-file", "--network=host"):
            self.assertNotIn(forbidden, command)
        with patch.dict("os.environ", {"AGENT_API_KEY": "secret", "ARBITRARY_SECRET": "secret"}):
            self.assertNotIn("AGENT_API_KEY", runner.environment())
            self.assertNotIn("ARBITRARY_SECRET", runner.environment())

    def test_disabled_runner_never_spawns_a_host_process(self):
        import asyncio
        runner = SandboxRunner(SandboxConfig(enabled=False))
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as process:
            result = asyncio.run(runner.run("python", "print(1)"))
            self.assertEqual(result["error"], "sandbox_disabled")
            process.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
