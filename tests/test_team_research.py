"""Models/network are simulated; real controller budgets and citation checks run."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agent_core.config import AgentError, Profile
from agent_core.providers import ModelTurn
from agent_core.team_research import TeamResearch, research_requested
from agent_core.tools import Tools as RealTools


def configuration(**changes):
    values = dict(max_parallel_workers=2, max_parallel_model_calls=1,
                  max_searches_per_worker=2, max_reads_per_worker=2,
                  max_sources=6, source_chars=1800, max_model_calls=5,
                  max_repairs=1, timeout_seconds=30)
    values.update(changes)
    return SimpleNamespace(**values)


TEXTS = {"researcher_1": "The official specification requires two independent checks before publishing the result.",
         "researcher_2": "The independent evaluation reports that two checks reduce errors but do not guarantee correctness."}
URLS = {"researcher_1": "https://primary.example/specification",
        "researcher_2": "https://evaluation.example/results"}


class Model:
    backend = SimpleNamespace(context_window=16384, max_output_tokens=1024)

    def __init__(self, tracker=None):
        self.calls, self.review_inputs = [], []
        self.tracker = tracker
        self.bad_quote, self.bad_review_count = False, 0
        self.contradiction = False

    async def complete_json(self, messages, schema):
        payload = json.loads(messages[-1]["content"])
        properties = schema["properties"]
        stage = "plan" if "workers" in properties else "review" if "contradictions" in properties else "analyst"
        self.calls.append((stage, payload))
        if self.tracker is not None:
            self.tracker["model_active"] += 1
            self.tracker["model_peak"] = max(self.tracker["model_peak"], self.tracker["model_active"])
            await asyncio.sleep(.005)
            self.tracker["model_active"] -= 1
        if stage == "plan":
            response = {"workers": [{"focus": "Primary specification", "queries": ["official specification"]},
                                    {"focus": "Independent evaluation", "queries": ["independent evaluation limitations"]}]}
        elif stage == "analyst":
            source = payload["data"]["sources"][0]
            response = {"findings": [{"text": source["excerpt"], "evidence": [{"source_id": source["id"], "quote": source["excerpt"]}]}], "gaps": []}
            if self.bad_quote:
                response["findings"][0]["evidence"][0]["quote"] = "A fabricated quote that was never read."
        else:
            self.review_inputs.append(payload["data"])
            findings = [finding for report in payload["data"]["reports"] for finding in report["shared_findings"]]
            response = {"findings": findings, "contradictions": [], "gaps": []}
            if self.bad_review_count:
                self.bad_review_count -= 1
                response["findings"] = [{"text": "Invented fact", "evidence": [{"source_id": 8, "quote": "A fabricated quote that was never read."}]}]
            if self.contradiction and len(findings) >= 2:
                response["contradictions"] = [{"text": "Las dos fuentes describen resultados distintos.",
                    "evidence": [findings[0]["evidence"][0], findings[1]["evidence"][0]]}]
        return ModelTurn(json.dumps(response))


class Tools:
    def __init__(self, worker_id, tracker=None, read_ok=True, failure=None):
        self.worker_id, self.tracker, self.read_ok, self.failure = worker_id, tracker, read_ok, failure
        self.searches, self.reads = [], []

    async def search(self, query):
        self.searches.append(query)
        if self.failure:
            raise self.failure
        if self.tracker is not None:
            self.tracker["active"] += 1
            self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
            await asyncio.sleep(.01)
            self.tracker["active"] -= 1
        return [{"id": 1, "url": URLS[self.worker_id], "title": self.worker_id,
                 "excerpt": "SEARCH SNIPPET IS NEVER EVIDENCE"}]

    async def _execute(self, name, arguments):
        assert name == "web_read"
        assert arguments["url"] == URLS[self.worker_id]
        self.reads.append(arguments["url"])
        if not self.read_ok:
            return {"ok": False, "error": "page_unavailable", "message": "Access denied"}
        return {"ok": True, "url": arguments["url"], "text": TEXTS[self.worker_id], "truncated": True}


class Checkpoints:
    def __init__(self):
        self.values = {}

    def __call__(self, key, value):
        self.values[key] = copy.deepcopy(value)

    def load(self, key):
        return copy.deepcopy(self.values.get(key))


class TeamResearchTest(unittest.IsolatedAsyncioTestCase):
    def factory(self, **options):
        instances = []

        def create(worker_id):
            tools = Tools(worker_id, **options)
            instances.append(tools)
            return tools
        return create, instances

    async def test_two_analysts_exchange_read_sources_and_critical_synthesis(self):
        factory, instances = self.factory()
        events, checkpoints = [], Checkpoints()
        model = Model()
        result = await TeamResearch(configuration(), model, factory,
            event=lambda *event: events.append(event), checkpointer=checkpoints,
            skill={"id": "research", "version": "1.2.0"}).run("Investiga la comprobación independiente")
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual([stage for stage, _ in model.calls], ["plan", "analyst", "analyst", "review"])
        self.assertEqual(result["review"], "team_sources_reviewed")
        self.assertEqual(result["team"]["skill_version"], "1.2.0")
        self.assertEqual(result["team"]["status"], "complete")
        self.assertEqual(len(instances), 2)
        self.assertIsNot(instances[0], instances[1])
        self.assertEqual([source["id"] for source in result["sources"]], [1, 2])
        self.assertTrue(all(source["retrieval"] == "page_extract" for source in result["sources"]))
        self.assertNotIn("SEARCH SNIPPET", json.dumps(result))
        self.assertIn("[1]", result["answer"])
        self.assertIn("[2]", result["answer"])
        self.assertEqual(len(model.review_inputs[0]["reports"]), 2)
        self.assertEqual(model.review_inputs[0]["reports"][1]["shared_findings"][0]["evidence"][0]["source_id"], 2)
        self.assertTrue({"worker_started", "worker_completed", "source_read", "team_review", "team_completed"}.issubset({event[0] for event in events}))
        self.assertIn("research:result", checkpoints.values)

    async def test_regex_plan_recovers_with_normal_queries_and_preserves_requested_site(self):
        class RegexPlan(Model):
            async def complete_json(self, messages, schema):
                result = await super().complete_json(messages, schema)
                if "workers" in schema["properties"]:
                    body = json.loads(result.content)
                    body["workers"][0]["queries"] = [r"\bsite:docs\.python\.org/\?s=unittest\.TestCase\s+assertEqual"]
                    return ModelTurn(json.dumps(body))
                return result
        factory, _ = self.factory()
        result = await TeamResearch(configuration(), RegexPlan(), factory).run("Busca en documentación oficial site:docs.python.org para qué sirven unittest.TestCase y assertEqual. Explica brevemente con fuentes leídas.")
        queries = [worker["queries"][0] for worker in result["team"]["workers"]]
        self.assertTrue(all("site:docs.python.org" in query and "unittest.TestCase" in query and "assertEqual" in query for query in queries))
        self.assertTrue(all("\\" not in query and "?s=" not in query for query in queries))
        self.assertNotEqual(queries[0], queries[1])
        self.assertEqual(result["team"]["status"], "complete")
        self.assertTrue(result["team"]["errors"][0]["resolved"])

    async def test_planner_cannot_drop_or_replace_a_site_filter(self):
        factory, _ = self.factory()
        result = await TeamResearch(configuration(), Model(), factory).run("Busca site:docs.python.org unittest")
        self.assertTrue(all("site:docs.python.org" in query for worker in result["team"]["workers"] for query in worker["queries"]))

    async def test_global_limits_apply_across_two_simultaneous_requests(self):
        tracker = {"active": 0, "peak": 0, "model_active": 0, "model_peak": 0}
        factories = [self.factory(tracker=tracker)[0] for _ in range(2)]
        teams = [TeamResearch(configuration(), Model(tracker), factory) for factory in factories]
        results = await asyncio.gather(*(team.run("Investiga con fuentes independientes") for team in teams))
        self.assertEqual(tracker["peak"], 2)
        self.assertEqual(tracker["model_peak"], 1)
        self.assertTrue(all(result["team"]["status"] == "complete" for result in results))

    async def test_dynamic_review_citations_do_not_mutate_templates_or_leak_to_next_request(self):
        class SchemaAudit(Model):
            def __init__(self):
                super().__init__()
                self.schemas = []

            async def complete_json(self, messages, schema):
                self.schemas.append(copy.deepcopy(schema))
                return await super().complete_json(messages, schema)

        first_factory, _ = self.factory()
        first_model = SchemaAudit()
        first_team = TeamResearch(configuration(), first_model, first_factory)
        analyst_template = copy.deepcopy(first_team._analyst_schema())
        reviewer_template = copy.deepcopy(first_team._review_schema())
        first = await first_team.run("Investiga la primera petición")
        self.assertEqual(first["team"]["status"], "complete")
        self.assertEqual(first_team._analyst_schema(), analyst_template)
        self.assertEqual(first_team._review_schema(), reviewer_template)

        second_texts = {"researcher_1": "SECOND_REQUEST_ALPHA has different evidence from the first request and must remain isolated.",
                        "researcher_2": "SECOND_REQUEST_BETA independently describes only the evidence read during this second request."}
        class SecondRequestTools(Tools):
            async def _execute(self, name, arguments):
                observed = await super()._execute(name, arguments)
                return {**observed, "text": second_texts[self.worker_id]}

        second_model = SchemaAudit()
        second_team = TeamResearch(configuration(), second_model, lambda worker_id: SecondRequestTools(worker_id))
        second = await second_team.run("Investiga la segunda petición")
        self.assertEqual(second["team"]["status"], "complete")
        self.assertEqual(second_team._analyst_schema(), analyst_template)
        self.assertEqual(second_team._review_schema(), reviewer_template)
        for schema in second_model.schemas:
            serialized = json.dumps(schema)
            self.assertNotIn(TEXTS["researcher_1"], serialized)
            self.assertNotIn(TEXTS["researcher_2"], serialized)
        for schema in second_model.schemas:
            if "findings" in schema["properties"] and "contradictions" not in schema["properties"]:
                evidence = schema["properties"]["findings"]["items"]["properties"]["evidence"]["items"]
                self.assertNotIn("anyOf", evidence)
                self.assertNotIn("enum", evidence["properties"]["quote"])
        review_schema = next(schema for schema in second_model.schemas if "contradictions" in schema["properties"])
        for name in ("findings", "contradictions"):
            choices = review_schema["properties"][name]["items"]["properties"]["evidence"]["items"]["anyOf"]
            pairs = {(choice["properties"]["source_id"]["enum"][0], choice["properties"]["quote"]["enum"][0]) for choice in choices}
            self.assertEqual(pairs, {(1, second_texts["researcher_1"]), (2, second_texts["researcher_2"])})

    async def test_blocked_pages_never_become_cited_snippets_or_claim_success(self):
        factory, _ = self.factory(read_ok=False)
        model = Model()
        result = await TeamResearch(configuration(), model, factory).run("Investiga esta cuestión")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["team"]["status"], "blocked")
        self.assertEqual(result["review"], "team_blocked")
        self.assertEqual(result["model_calls"], 1)
        self.assertNotIn("SEARCH SNIPPET", result["answer"])
        self.assertTrue(all(worker["status"] == "blocked" for worker in result["team"]["workers"]))
        self.assertEqual(result["team"]["workers"][0]["errors"][0]["url"], URLS["researcher_1"])

    async def test_unobserved_analyst_quotes_are_rejected_without_false_final_review(self):
        factory, _ = self.factory()
        model = Model()
        model.bad_quote = True
        result = await TeamResearch(configuration(), model, factory).run("Busca evidencia")
        self.assertEqual(result["team"]["status"], "partial")
        self.assertEqual(result["model_calls"], 3)
        self.assertIsNone(result["team"]["reports"])
        self.assertNotIn("fabricated", result["answer"])
        self.assertIn("La revisión crítica no se completó", result["answer"])
        self.assertTrue(all(worker["errors"][0]["code"] == "team_invalid_evidence" for worker in result["team"]["workers"]))

    async def test_bad_reviewer_citations_get_one_repair_with_exact_total_budget(self):
        factory, _ = self.factory()
        model = Model()
        model.bad_review_count = 2
        result = await TeamResearch(configuration(), model, factory).run("Investiga")
        self.assertEqual(result["model_calls"], 5)
        self.assertEqual(result["team"]["status"], "partial")
        self.assertNotIn("Invented fact", result["answer"])
        self.assertEqual(len([stage for stage, _ in model.calls if stage == "review"]), 2)

    async def test_successful_repair_is_recorded_and_can_finish_complete(self):
        factory, _ = self.factory()
        model = Model()
        model.bad_review_count = 1
        result = await TeamResearch(configuration(), model, factory).run("Investiga")
        self.assertEqual(result["model_calls"], 5)
        self.assertEqual(result["team"]["status"], "complete")
        self.assertEqual(result["team"]["errors"][0]["resolved"], True)
        self.assertNotIn("Invented fact", result["answer"])

    async def test_review_records_contradictions_instead_of_claiming_complete(self):
        factory, _ = self.factory()
        model = Model()
        model.contradiction = True
        result = await TeamResearch(configuration(), model, factory).run("Investiga")
        self.assertEqual(result["team"]["status"], "partial")
        self.assertIn("Contradicción pendiente", result["answer"])
        self.assertEqual(len(result["team"]["reports"]["contradictions"]), 1)

    async def test_successful_phases_resume_without_repeating_network_or_model(self):
        checkpoints = Checkpoints()
        factory, _ = self.factory()
        first = await TeamResearch(configuration(), Model(), factory, checkpointer=checkpoints).run("Investiga")
        self.assertEqual(first["model_calls"], 4)
        def no_tools(worker_id):
            self.fail("Completed worker should be restored, not execute tools")
        model = Model()
        second = await TeamResearch(configuration(), model, no_tools, checkpointer=checkpoints).run("Investiga")
        self.assertEqual(second["model_calls"], 0)
        self.assertEqual(second["answer"], first["answer"])
        self.assertEqual(model.calls, [])
        self.assertEqual(set(second["team"]["reused_phases"]), {"plan", "researcher_1", "researcher_2", "review"})

    async def test_partial_resume_rechecks_review_when_one_worker_is_reexecuted(self):
        checkpoints = Checkpoints()
        factory, _ = self.factory()
        await TeamResearch(configuration(), Model(), factory, checkpointer=checkpoints).run("Investiga")
        del checkpoints.values["research:researcher_2"]
        factory, instances = self.factory()
        model = Model()
        result = await TeamResearch(configuration(), model, factory, checkpointer=checkpoints).run("Investiga")
        self.assertEqual([tool.worker_id for tool in instances], ["researcher_2"])
        self.assertEqual([stage for stage, _ in model.calls], ["analyst", "review"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["team"]["status"], "complete")

    async def test_real_tools_keep_worker_sources_separate_and_use_guarded_reader(self):
        instances = []
        def search(request):
            worker_id = "researcher_1" if "official" in request.url.params["q"] else "researcher_2"
            return httpx.Response(200, json={"results": [{"url": URLS[worker_id], "title": "Official specification evaluation",
                "content": "Search snippets must be replaced by observed page text"}]})
        def factory(worker_id):
            tools = RealTools(None, 99, 2, httpx.MockTransport(search))
            tools.reader = SimpleNamespace(read=AsyncMock(return_value={"ok": True, "url": URLS[worker_id], "text": TEXTS[worker_id], "truncated": False}))
            instances.append(tools)
            return tools
        with patch.dict("os.environ", {"SEARXNG_URL": "http://127.0.0.1:8888"}):
            result = await TeamResearch(configuration(), Model(), factory).run("Investiga")
        self.assertEqual(result["team"]["status"], "complete")
        self.assertTrue(all(tool.user_id == 99 for tool in instances))
        self.assertIsNot(instances[0].sources, instances[1].sources)
        self.assertEqual(instances[0].reader.read.await_count, 1)
        self.assertEqual(instances[1].reader.read.await_count, 1)
        self.assertEqual({tool.sources[0]["retrieval"] for tool in instances}, {"page_extract"})

    async def test_timeout_cancels_both_workers_and_never_claims_success(self):
        waiting = asyncio.Event()
        class WaitingTools(Tools):
            async def search(self, query):
                await waiting.wait()
                return []
        events = []
        team = TeamResearch(configuration(), Model(), lambda worker_id: WaitingTools(worker_id),
                            event=lambda *event: events.append(event))
        team.timeout = .01
        result = await team.run("Investiga")
        self.assertTrue(result["team"]["timed_out"])
        self.assertEqual(result["team"]["status"], "blocked")
        self.assertEqual([worker["status"] for worker in result["team"]["workers"]], ["interrupted", "interrupted"])
        self.assertEqual(len([event for event in events if event[0] == "worker_completed"]), 2)

    async def test_unexpected_worker_error_is_safe_and_other_worker_still_finishes(self):
        def factory(worker_id):
            return Tools(worker_id, failure=RuntimeError("SECRET_TOKEN_ABC") if worker_id == "researcher_1" else None)
        result = await TeamResearch(configuration(), Model(), factory).run("Investiga")
        self.assertEqual(result["team"]["status"], "partial")
        self.assertEqual(len(result["sources"]), 1)
        self.assertNotIn("SECRET_TOKEN", json.dumps(result))
        self.assertTrue(any(error["code"] == "team_worker_failed" for error in result["team"]["errors"]))

    async def test_tools_must_be_independent_and_cannot_share_source_list(self):
        shared = Tools("researcher_1")
        result = await TeamResearch(configuration(), Model(), lambda _: shared).run("Investiga")
        self.assertEqual(result["team"]["status"], "partial")
        self.assertTrue(any(error["code"] == "team_tools_not_isolated" for error in result["team"]["errors"]))

    async def test_model_budget_stops_analysis_and_review_without_fake_completion(self):
        factory, _ = self.factory()
        result = await TeamResearch(configuration(max_model_calls=2), Model(), factory).run("Investiga")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["team"]["status"], "partial")
        self.assertTrue(any(error["code"] == "team_model_budget" for error in result["team"]["errors"]))

    async def test_planner_history_excludes_private_documents_secrets_and_unrelated_turns(self):
        history = [{"role": "user", "content": "Busca en mis documentos el presupuesto privado 123456"},
                   {"role": "user", "content": "Mi contraseña es SECRET_SENTINEL"},
                   {"role": "user", "content": "Busca documentación oficial de Python"},
                   {"role": "assistant", "content": "Contenido privado del asistente"},
                   {"role": "user", "content": "Busca con token SECRET_API_TOKEN"},
                   {"role": "user", "content": "Recuerda mi nota personal"}]
        factory, _ = self.factory()
        model = Model()
        await TeamResearch(configuration(), model, factory).run("Y para otra versión?", history)
        observed = model.calls[0][1]["previous_user_requests"]
        self.assertEqual(observed, ["Busca documentación oficial de Python"])
        self.assertNotIn("SECRET", json.dumps(model.calls))

    def test_router_keeps_greetings_downloads_and_unsupported_profiles_out(self):
        profile = Profile(name="Research", description="Research", prompt="test", research=True, tools=["web_search", "web_read"])
        for message in ("hola", "ola como estas", "Gracias", "descarga el vídeo https://youtube.com/x", "No busques en internet", "Responde sin consultar internet"):
            self.assertFalse(research_requested(message, profile=profile), message)
        for message in ("Busca en mis documentos el contrato", "Consulta mi memoria", "Busca los archivos del proyecto", "Consulta mi historial"):
            self.assertFalse(research_requested(message, profile=profile), message)
        self.assertTrue(research_requested("Busca en la web y en mis documentos", profile=profile))
        self.assertTrue(research_requested("¿Qué dice la documentación?", profile=profile))
        self.assertTrue(research_requested("Busca la documentación oficial", profile=profile))
        self.assertTrue(research_requested("Y en Python?", [{"role": "user", "content": "Busca la documentación de Java"}], profile))
        profile.research = False
        self.assertFalse(research_requested("Explica dos más dos", profile=profile))
        profile.tools = ["calculator"]
        self.assertFalse(research_requested("Busca documentación", profile=profile))


if __name__ == "__main__":
    unittest.main()
