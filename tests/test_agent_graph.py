"""Control del DAG con herramientas observadas, SQLite y HTTP; modelo y Docker simulados."""
import json
import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.config import AgentError
from agent_core.providers import Provider
from agent_core.task_graph import TaskGraph
from agent_core.relations import research_graph
from agent_core.tools import Tools
from agent_core.service import compact_observation, context_cost, fit_tool_context


def step(key, tool, deps=()):
    return {"id": key, "title": "Comprobar " + key, "tool": tool, "depends_on": list(deps)}


class TaskGraphTest(unittest.TestCase):
    def test_tool_context_compacts_complete_native_exchanges_and_keeps_failures(self):
        messages = [{"role": "system", "content": "Instrucciones originales"},
                    {"role": "user", "content": "Comprueba el archivo"}]
        for index in range(3):
            call_id = str(index)
            messages.extend([
                {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "function": {
                    "name": "sandbox_exec", "arguments": json.dumps({"code": "#" + "x" * 5000})}}]},
                {"role": "tool", "tool_call_id": call_id, "tool_name": "sandbox_exec", "content": json.dumps({
                    "ok": False, "exit_code": 1, "attempt_id": "attempt-" + call_id,
                    "stderr": "Traceback\n" + "x" * 3000 + "\nModuleNotFoundError: No module named 'cumbia'"})}])
        fitted = fit_tool_context(messages, 6000, lambda items: context_cost(items, []))
        self.assertLessEqual(context_cost(fitted, []), 6000)
        self.assertEqual(fitted[:2], messages[:2])
        self.assertEqual(len(messages), 8)  # No modifica el historial detallado.
        compacted = json.dumps(fitted, ensure_ascii=False)
        for index in range(3):
            self.assertIn("attempt-" + str(index), compacted)
        self.assertIn("ModuleNotFoundError", compacted)
        for index, message in enumerate(fitted):
            if message["role"] == "tool":
                self.assertEqual(fitted[index - 1]["tool_calls"][0]["id"], message["tool_call_id"])

    def test_output_compaction_keeps_the_last_error_and_reports_truncation(self):
        text = "Traceback\n" + "x" * 3000 + "\nModuleNotFoundError: missing"
        observation = compact_observation({"stderr": text})
        self.assertLessEqual(len(observation["stderr"]), 1000)
        self.assertTrue(observation["stderr"].endswith("ModuleNotFoundError: missing"))
        self.assertIn("truncado", observation["stderr"])

    def test_tool_context_rejects_a_request_that_cannot_fit_without_losing_it(self):
        messages = [{"role": "system", "content": "Instrucciones"}, {"role": "user", "content": "x" * 8000}]
        with self.assertRaises(AgentError) as caught:
            fit_tool_context(messages, 4096, lambda items: context_cost(items, []))
        self.assertEqual(caught.exception.code, "context_limit")
        self.assertEqual(len(messages[-1]["content"]), 8000)

    def test_source_graph_preserves_the_exact_jaccard_fraction(self):
        graph = research_graph([{"id": 1, "title": "alpha beta", "excerpt": ""},
                                {"id": 2, "title": "alpha gamma", "excerpt": ""}], .3)
        self.assertEqual(graph["edges"][0]["similarity_exact"], {"numerator": 1, "denominator": 3})
        self.assertEqual(graph["edges"][0]["intersection_terms"], 1)
        self.assertEqual(graph["edges"][0]["union_terms"], 3)
        self.assertEqual(graph["statistics"]["beta_1"], 0)

    def test_topological_order_and_dependencies_are_enforced(self):
        graph = TaskGraph()
        graph.plan([step("leer", "web_read", ["buscar"]), step("buscar", "web_search")], ["web_read", "web_search"])
        with self.assertRaises(AgentError) as caught:
            graph.before("web_read")
        self.assertEqual(caught.exception.code, "dependency_pending")
        graph.record(graph.before("web_search"), {"sources": [{"id": 1}]})
        graph.record(graph.before("web_read"), {"ok": True})
        self.assertFalse(graph.pending)
        self.assertEqual([n["id"] for n in graph.snapshot()["nodes"]], ["buscar", "leer"])

    def test_cycle_unknown_dependency_and_unpermitted_tool_are_rejected_atomically(self):
        for plan in ([step("a", "calculator", ["a"])], [step("a", "calculator", ["missing"])],
                     [step("a", "shell_host")], [step("a", "calculator"), step("a", "calculator")]):
            graph = TaskGraph()
            with self.assertRaises(AgentError):
                graph.plan(plan, ["calculator"])
            self.assertEqual(graph.nodes, {})

    def test_repeated_upstream_action_invalidates_all_descendants(self):
        graph = TaskGraph()
        graph.plan([step("a", "web_search"), step("b", "web_read", ["a"]), step("c", "calculator", ["b"])],
                   ["web_search", "web_read", "calculator"])
        for tool in ("web_search", "web_read", "calculator"):
            graph.record(graph.before(tool), {"ok": True, "sources": [1]})
        self.assertFalse(graph.pending)
        graph.record(graph.before("web_search"), {"sources": [1]})
        self.assertEqual([n["status"] for n in graph.nodes.values()], ["observed", "pending", "pending"])

    def test_empty_search_and_failed_actions_never_complete_a_step(self):
        graph = TaskGraph()
        graph.plan([step("buscar", "web_search")], ["web_search"])
        graph.record(graph.before("web_search"), {"sources": []})
        self.assertTrue(graph.pending)
        graph.record(graph.before("web_search"), {"ok": False, "error": "timeout"})
        self.assertEqual(graph.nodes["buscar"]["attempts"], 2)
        self.assertEqual(graph.nodes["buscar"]["status"], "failed")

    def test_a_plan_cannot_mark_itself_done_or_skip_project_execution(self):
        for plan in ([{**step("a", "calculator"), "status": "observed"}], [step("a", "project_write")],
                     [step("calc" + str(i), "calculator") for i in range(6)]):
            with self.assertRaises(AgentError):
                TaskGraph().plan(plan, ["calculator", "project_write", "sandbox_exec"])


class AgentGraphIntegrationTest(unittest.TestCase):
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    tearDown = fixtures.ProjectTest.tearDown

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode, self.scenario, self.model_step = "tools", "complete", 0
        self.native_actions = False
        self.runner = SimpleNamespace(run=AsyncMock(return_value={"ok": True, "exit_code": 0, "stdout": "4\n", "stderr": ""}))
        patcher = patch("agent_core.tools.SandboxRunner", return_value=self.runner)
        patcher.start(); self.addCleanup(patcher.stop)

    async def upstream(self, request):
        if request.url.path != "/api/chat":
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        if not self.native_actions:
            self.assertIn("format", body)
            self.assertNotIn("tools", body)
        self.model_step += 1
        name, arguments = None, None
        if self.scenario == 'invalid_plan_loop':
            only_answer = body.get('format', {}).get('anyOf', [{}])[0].get('required') == ['answer']
            action = {'answer': 'Resultado parcial del modelo.'} if only_answer else {
                'tool': 'task_plan', 'arguments': {'steps': [step('guardar', 'project_write')]}}
            return httpx.Response(200, json={'message': {'content': json.dumps(action)}})
        if self.scenario == "clarify":
            return httpx.Response(200, json={"message": {"content": json.dumps({"answer": "¿Qué canción o artista de cumbia quieres descargar? Puedo ayudarte a encontrar una fuente."})}})
        if self.model_step == 1:
            if not self.native_actions:
                offered = [name for branch in body['format']['anyOf'] for name in branch.get('properties', {}).get('tool', {}).get('enum', [])]
                self.assertEqual(offered, ['task_plan', 'tool_discover'])
            name, arguments = "task_plan", {"steps": [step("guardar", "project_write"), step("probar", "sandbox_exec", ["guardar"])]}
        elif self.scenario != "no_actions" and self.model_step == 2:
            name, arguments = "project_write", {"files": [{"path": "main.py", "content": "assert 2+2 == 4\nprint(4)\n"}]}
        elif self.scenario not in {"no_actions", "no_test"} and self.model_step == 3:
            name, arguments = "sandbox_exec", {"language": "python", "code": "import main", "purpose": "Ejecutar archivo", "project": self.scenario != "wrong_snapshot"}
        elif self.scenario == "changed_after_test" and self.model_step == 4:
            name, arguments = "project_write", {"files": [{"path": "main.py", "content": "raise RuntimeError('sin comprobar')\n"}]}
        message = {"content": json.dumps({"answer": "Resultado final del modelo simulado."})}
        if name:
            message = {"content": json.dumps({"tool": name, "arguments": arguments} if "anyOf" in body.get("format", {}) else arguments)}
        if self.native_actions:
            message = {"content": "El modelo afirma que ejecutó todo, aunque no haya evidencias."}
            if name:
                message = {"content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}
        return httpx.Response(200, json={"message": message})

    def run_scenario(self, scenario="complete", expected_status=200):
        self.scenario = scenario
        headers = self.register()
        conv = self.conversation(headers, "agente")
        self.request_id = str(uuid.uuid4())
        if scenario == "complete":
            response = self.send(headers, conv, "Crea y ejecuta un programa con una comprobación", self.request_id)
        else:
            # Ejercita también el camino de llamadas nativas usado por APIs compatibles.
            self.native_actions = True
            async def native(provider, messages, schemas, *, allow_answer=False):
                return await provider.complete(messages, schemas)
            with patch.object(Provider, "structured_action", new=native):
                response = self.send(headers, conv, "Crea y ejecuta un programa con una comprobación", self.request_id)
        self.assertEqual(response.status_code, expected_status, response.text)
        return headers, conv, response.json()

    def test_agent_plans_executes_and_returns_an_observed_graph(self):
        headers, conv, result = self.run_scenario()
        self.assertTrue(result["task_graph"]["complete"])
        self.assertEqual(result["review"], "model_reviewed")
        self.assertFalse(result["project_unverified"])
        self.assertEqual(result["tested_project"][0]["path"], "main.py")
        self.assertEqual(result["tools_used"], ["task_plan", "project_write", "sandbox_exec"])
        activity = self.client.get(f"/api/agent/conversations/{conv}/activity").json()
        self.assertTrue(any(e["kind"] == "task_graph" for e in activity["events"]))
        calls = self.model_step
        replay = self.send(headers, conv, "Crea y ejecuta un programa con una comprobación", self.request_id)
        self.assertEqual(replay.json(), result)
        self.assertEqual(self.model_step, calls)
        self.assertEqual(self.runner.run.await_count, 1)

    def test_agent_can_ask_for_a_song_without_forcing_a_plan_or_python(self):
        self.scenario = "clarify"
        headers = self.register()
        conv = self.conversation(headers, "agente")
        response = self.send(headers, conv, "quiero descargar una cumbia")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIn("canción o artista", result["answer"])
        self.assertEqual(result["tools_used"], [])
        self.assertEqual(result["review"], "model_reviewed")
        self.assertIsNone(result["task_graph"])
        self.runner.run.assert_not_awaited()
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/project").json()["files"], [])
        last_request = next(body for path, body, _ in reversed(self.requests) if path == "/api/chat")
        self.assertEqual(last_request["format"]["anyOf"][0]["required"], ["answer"])

    def test_repeated_invalid_plans_finish_with_an_honest_partial_reply(self):
        self.scenario = 'invalid_plan_loop'
        headers = self.register(); conv = self.conversation(headers, 'agente')
        response = self.send(headers, conv, 'Crea y ejecuta un programa Python que imprima 4')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['review'], 'unresolved')
        self.assertIn('no se consiguió un plan válido', result['answer'])
        self.assertEqual(self.model_step, self.config()['limits']['max_model_calls'])
        self.assertEqual(result['tools_used'], ['task_plan'] * (self.model_step - 1))
        self.assertEqual(len(self.client.get(f'/api/agent/conversations/{conv}/messages').json()), 2)
        activity = self.client.get(f'/api/agent/conversations/{conv}/activity').json()
        self.assertEqual(activity['status'], 'complete')
        self.assertTrue(any(event['kind'] == 'budget_handoff' for event in activity['events']))
        self.runner.run.assert_not_awaited()

    def test_finished_actions_can_report_without_budget_for_another_review(self):
        config = self.config(); config['limits']['max_model_calls'] = 4; self.save_config(config)
        headers, conv, result = self.run_scenario()
        self.assertEqual(result['review'], 'unresolved', 'La revisión omitida no debe presentarse como realizada')
        self.assertTrue(result['task_graph']['complete'])
        self.assertFalse(result['project_unverified'])
        self.assertEqual(self.runner.run.await_count, 1)
        self.assertEqual(len(self.client.get(f'/api/agent/conversations/{conv}/messages').json()), 2)

    def test_tool_budget_preserves_pending_steps_and_returns_without_retrying(self):
        config = self.config(); config['limits']['max_tool_calls'] = 2; self.save_config(config)
        original_upstream = self.upstream
        async def respect_final_answer(request):
            if request.url.path == '/api/chat':
                body = json.loads(request.content)
                if body.get('format', {}).get('anyOf', [{}])[0].get('required') == ['answer']:
                    self.model_step += 1
                    return httpx.Response(200, json={'message': {'content': json.dumps({'answer': 'La tarea está completa.'})}})
            return await original_upstream(request)
        self.app.state.agent.transport = httpx.MockTransport(respect_final_answer)
        headers, conv, result = self.run_scenario()
        self.assertEqual(self.model_step, 3)
        self.assertEqual(result['review'], 'unresolved')
        self.assertFalse(result['task_graph']['complete'])
        self.assertTrue(result['project_unverified'])
        self.assertIn('La tarea quedó pendiente', result['answer'])
        self.assertNotIn('La tarea está completa', result['answer'])
        self.assertEqual(result['tools_used'], ['task_plan', 'project_write'])
        activity = self.client.get(f'/api/agent/conversations/{conv}/activity').json()
        self.assertFalse(any(event['kind'] == 'action_required' for event in activity['events']))
        self.runner.run.assert_not_awaited()

    def test_a_large_script_finishes_with_compaction_and_full_activity_is_retained(self):
        original_upstream = self.upstream
        async def with_large_script(request):
            response = await original_upstream(request)
            if request.url.path == "/api/chat" and self.model_step == 2:
                content = "#" + "x" * 7600 + "\nprint(4)\n"
                self.large_content = content
                return httpx.Response(200, json={"message": {"content": json.dumps({"files": [{"path": "main.py", "content": content}]})}})
            return response
        self.app.state.agent.transport = httpx.MockTransport(with_large_script)
        headers, conv, result = self.run_scenario()
        self.assertEqual(result["review"], "model_reviewed")
        self.assertTrue(result["task_graph"]["complete"])
        activity = self.client.get(f"/api/agent/conversations/{conv}/activity").json()
        self.assertEqual(activity["status"], "complete")
        stored = self.client.get(f"/api/agent/conversations/{conv}/project").json()["files"]
        self.assertEqual(stored[0]["content"], self.large_content)
        self.assertTrue(any("Resumen de acciones ya ejecutadas" in json.dumps(body["messages"], ensure_ascii=False)
                            for path, body, _ in self.requests if path == "/api/chat"))
        for path, body, _ in self.requests:
            if path == "/api/chat":
                self.assertLessEqual(context_cost(body["messages"], body["format"]) + 1024 + 256, 16384)

    def test_a_textual_claim_never_completes_pending_steps(self):
        _, _, result = self.run_scenario("no_actions")
        self.assertEqual(result["review"], "unresolved")
        self.assertFalse(result["task_graph"]["complete"])
        self.assertNotIn("El modelo afirma", result["answer"])
        self.runner.run.assert_not_awaited()

    def assert_unverified(self, scenario):
        _, _, result = self.run_scenario(scenario)
        self.assertEqual(result["review"], "unresolved")
        self.assertTrue(result["project_unverified"])

    def test_saved_files_are_pending_until_executed(self):
        self.assert_unverified("no_test")

    def test_execution_without_project_does_not_verify_saved_files(self):
        self.assert_unverified("wrong_snapshot")

    def test_a_finished_plan_does_not_accept_unnecessary_writes(self):
        _, _, result = self.run_scenario("changed_after_test", 502)
        self.assertEqual(result["code"], "unexpected_tool_call")

    def test_a_later_write_invalidates_a_previously_tested_snapshot(self):
        headers = self.register()
        conv = self.conversation(headers, "laboratorio")
        tools = Tools(self.app.state.database, 1, 2, conversation_id=conv, runner=self.runner)
        async def exercise():
            await tools.execute("project_write", {"files": [{"path": "main.py", "content": "print(4)"}]}, ["project_write"])
            await tools.execute("sandbox_exec", {"language": "python", "code": "import main", "purpose": "Comprobar", "project": True}, ["sandbox_exec"])
            self.assertFalse(tools.project_dirty)
            previous = tools.tested_project[0]["sha256"]
            restarted = Tools(self.app.state.database, 1, 2, conversation_id=conv, runner=self.runner)
            self.assertFalse(restarted.project_dirty)
            self.assertEqual(restarted.tested_project[0]["sha256"], previous)
            await tools.execute("project_write", {"files": [{"path": "main.py", "content": "print(5)"}]}, ["project_write"])
            self.assertTrue(tools.project_dirty)
            self.assertEqual(tools.tested_project[0]["sha256"], previous)
            restarted = Tools(self.app.state.database, 1, 2, conversation_id=conv, runner=self.runner)
            self.assertTrue(restarted.project_dirty)
            self.assertEqual(restarted.tested_project, [])
        asyncio.run(exercise())

    def test_incomplete_python_is_rejected_before_any_file_is_saved(self):
        headers = self.register()
        conv = self.conversation(headers, "agente")
        tools = Tools(self.app.state.database, 1, 2, conversation_id=conv, runner=self.runner, task_planning=True)
        tools.task_graph.plan([step("guardar", "project_write"), step("probar", "sandbox_exec", ["guardar"])], ["project_write", "sandbox_exec"])
        with self.assertRaises(AgentError) as caught:
            asyncio.run(tools.execute("project_write", {"files": [{"path": "README.md", "content": "No guardar este lote"},
                                                                      {"path": "main.py", "content": "print("}]}, ["project_write"]))
        self.assertEqual(caught.exception.code, "python_syntax")
        self.assertEqual(tools.project.read(), [])
        self.assertEqual(tools.task_graph.nodes["guardar"]["status"], "failed")
        self.runner.run.assert_not_awaited()

