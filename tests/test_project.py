"""Rutas y SQLite reales; proveedores y buscador simulados con HTTPX MockTransport."""
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import create_app
from agent_core.config import AgentError, Settings, validate_server_url
from agent_core.security import COOKIE_NAME, verify_password
from agent_core.service import build_context
from agent_core.storage import Database
from agent_core.tools import calculate, public_source_url


PROJECT = Path(__file__).resolve().parent.parent


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("config", "profiles", "prompts", "templates", "docs", "agent_skills"):
            shutil.copytree(PROJECT / name, self.root / name)
        (self.root / "static").mkdir()
        self.settings = Settings(self.root, self.root / "test.sqlite3")
        self.configure_model()
        self.requests = []
        self.mode = "text"
        self.entered = threading.Event()
        self.environment = patch.dict(os.environ, {"AGENT_API_KEY": "fixture-secret-only-on-server", "SEARXNG_URL": "https://search.example.test"})
        self.environment.start()
        self.transport = httpx.MockTransport(self.upstream)
        self.app = create_app(self.settings, self.transport)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()
        self.temp.cleanup()

    def config(self):
        return json.loads((self.root / "config/agent.json").read_text("utf-8"))

    def save_config(self, config):
        (self.root / "config/agent.json").write_text(json.dumps(config), "utf-8")
        if hasattr(self, "app"):
            self.app.state.agent.capabilities.clear()

    def configure_model(self, adapter="ollama", **changes):
        config = self.config()
        # Estas pruebas cubren el controlador individual; el equipo tiene su batería propia.
        config.setdefault("multi_agent", {})["enabled"] = False
        config['downloads']['persistent_recovery'] = False
        config["backends"]["local"].update({"adapter": adapter, "model": "fixture-model",
            "base_url": "http://127.0.0.1:11434" if adapter == "ollama" else "https://model.example.test/v1",
            "api_key_env": "AGENT_API_KEY", "verify_tools": False, "temperature": None, **changes})
        self.save_config(config)

    def test_explicit_tool_discovery_happens_before_a_text_only_model_can_finish(self):
        headers = self.register()
        conversation = self.conversation(headers, 'general')
        response = self.send(headers, conversation, 'Usa exclusivamente tool_discover para identificar capacidades locales.')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIn('tool_discover', result['tools_used'])
        events = self.app.state.database.activity(1, conversation, result['activity_request_id'])['events']
        mandatory = next(event for event in events if event['payload'].get('executor') == 'server_required_action')
        first_model = next(event for event in events if event['kind'] == 'model')
        self.assertLess(mandatory['id'], first_model['id'])

    async def upstream(self, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.url.path, body, dict(request.headers)))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"] + (["tools"] if self.mode in {"tools", "forbidden_tool", "endless", "probe", "web_search", "chat_tools"} else []),
                                              "model_info": {"fixture.context_length": 16384}})
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"title": "Referencia primaria de prueba", "url": "https://docs.example.org/referencia", "content": "Una fuente de prueba respaldada por un resultado simulado."},
                {"title": "No permitido", "url": "javascript:alert(1)", "content": "ignorar"},
                {"title": "Local", "url": "http://127.0.0.1/secreto", "content": "ignorar"}]})
        if request.url.path not in {"/api/chat", "/v1/chat/completions"}:
            return httpx.Response(404)
        if self.mode == "failure":
            return httpx.Response(401, text="fixture-secret-only-on-server NO EXPONER")
        if self.mode == "nonjson":
            return httpx.Response(200, text="not-json")
        if self.mode == "timeout":
            raise httpx.ReadTimeout("fixture-secret-only-on-server", request=request)
        if self.mode == "slow":
            self.entered.set()
            await asyncio.sleep(.25)
        if self.mode == "run_timeout":
            await asyncio.sleep(10)
        messages = body["messages"]
        if self.mode == "probe" and body.get("tools") and body["tools"][0]["function"]["name"] == "capability_probe":
            message = {"content": "", "tool_calls": [{"id": "fixture-probe", "function": {"name": "capability_probe", "arguments": {"value": "ok"}}}]}
        elif self.mode == "web_search" and not any(m["role"] == "tool" for m in messages):
            message = {"content": "", "tool_calls": [{"id": "fixture-search", "function": {"name": "web_search", "arguments": {"query": "Python unittest documentación oficial"}}}]}
        elif self.mode in {"tools", "forbidden_tool", "endless"} and (self.mode == "endless" or not any(m["role"] == "tool" for m in messages)):
            name = "read_any_file" if self.mode == "forbidden_tool" else "calculator"
            message = {"content": "", "tool_calls": [{"id": "fixture-tool", "function": {"name": name, "arguments": {"expression": "(6 + 3) * 2"}}}]}
        else:
            message = {"content": "Respuesta SIMULADA: " + next(m["content"] for m in reversed(messages) if m["role"] == "user")}
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": message, "done": True})
        return httpx.Response(200, json={"choices": [{"message": message}]})

    def register(self, username="alice"):
        response = self.client.post("/auth", data={"username": username, "password": "prueba-segura-123", "password_confirm": "prueba-segura-123", "email": username + '@example.invalid', "action": "register"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        session = self.client.get("/api/session").json()
        return {"X-CSRF-Token": session["csrf_token"]}

    def conversation(self, headers, profile="general"):
        response = self.client.post("/api/agent/conversations", headers=headers, json={"profile": profile})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def send(self, headers, conversation, message="Hola agente", request_id=None):
        return self.client.post(f"/api/agent/conversations/{conversation}/messages", headers=headers,
                                json={"message": message, "request_id": request_id or str(uuid.uuid4())})

    def test_anonymous_and_forged_legacy_cookie_are_rejected(self):
        self.client.cookies.set("session_user", "alice")
        for path in ("/api/messages", "/api/agent/config", "/api/agent/conversations", "/api/agent/memory"):
            self.assertEqual(self.client.get(path).status_code, 401)
        page = self.client.get("/")
        self.assertIn("login.mp4", page.text)
        self.assertIn("logo.jpg", page.text)

    def test_registration_password_verification_and_cookie(self):
        headers = self.register()
        with self.app.state.database.connection() as c:
            encoded = c.execute("SELECT password FROM users").fetchone()[0]
        self.assertNotEqual(encoded, "prueba-segura-123")
        self.assertTrue(verify_password("prueba-segura-123", encoded))
        response = self.client.post("/auth", data={"username": "alice", "password": "incorrecta"}, follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/auth", data={"username": "alice", "password": "prueba-segura-123"}, follow_redirects=False)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.post("/api/messages", headers=headers, json={"message": "sesión anterior"}).status_code, 403)

    def test_registration_is_explicit_and_requires_password_length(self):
        response = self.client.post("/auth", data={"username": "new", "password": "abcdefgh"}, follow_redirects=False)
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/auth", data={"username": "new", "password": "x", "action": "register"}, follow_redirects=False)
        self.assertEqual(response.status_code, 422)

    def test_csrf_origin_body_limit_and_safe_validation(self):
        headers = self.register()
        self.assertEqual(self.client.post("/api/messages", json={"message": "sin token"}).status_code, 403)
        self.assertEqual(self.client.post("/api/messages", headers={**headers, "Origin": "https://otro.example"}, json={"message": "otro origen"}).status_code, 403)
        self.assertEqual(self.client.post("/api/messages", headers=headers, content=b"x" * 140000).status_code, 413)
        self.assertEqual(self.client.post("/api/messages", headers=headers, content=iter([b"x" * 70000, b"x" * 70000])).status_code, 413)
        response = self.client.post("/auth", data={"username": "a", "password": "no-reflejar-esta-clave", "action": "invalid"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("no-reflejar-esta-clave", response.text)
        self.assertEqual(self.client.get("/api/messages").json(), [])

    def test_logout_revokes_session_and_expiry(self):
        self.register()
        old_cookie = self.client.cookies.get(COOKIE_NAME)
        csrf = self.client.get("/api/session").json()["csrf_token"]
        self.assertEqual(self.client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code, 303)
        self.client.cookies.set(COOKIE_NAME, old_cookie)
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.client.cookies.clear()
        self.client.post("/auth", data={"username": "alice", "password": "prueba-segura-123"})
        with self.app.state.database.connection() as c:
            c.execute("UPDATE sessions SET expires_at=0")
        self.assertEqual(self.client.get("/api/session").status_code, 401)

    def test_community_still_shared_but_agent_is_separate(self):
        alice = self.register()
        self.assertEqual(self.client.post("/api/messages", headers=alice, json={"message": "hola comunidad"}).status_code, 200)
        conv = self.conversation(alice)
        self.assertEqual(self.send(alice, conv, "secreto privado").status_code, 200)
        self.client.cookies.clear()
        self.register("bob")
        community = self.client.get("/api/messages").json()
        self.assertEqual([m["message"] for m in community if m["role"] == "user"], ["hola comunidad"])
        self.assertNotIn("secreto privado", json.dumps(community))
        self.assertEqual(self.client.get("/api/agent/conversations").json(), [])

    def test_two_users_cannot_read_write_or_delete_each_others_data(self):
        alice = self.register()
        conv = self.conversation(alice)
        document = self.client.post("/api/agent/memory", headers=alice, json={"title": "Plan", "content": "Mi plan secreto violeta."}).json()["id"]
        self.client.cookies.clear()
        bob = self.register("bob")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/messages").status_code, 404)
        self.assertEqual(self.send(bob, conv).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/agent/memory/{document}", headers=bob).status_code, 404)
        self.assertEqual(self.client.get("/api/agent/memory").json(), [])
        self.assertEqual(self.requests, [])

    def test_context_keeps_own_history_and_excludes_other_conversations(self):
        headers = self.register()
        first, second = self.conversation(headers), self.conversation(headers)
        self.assertEqual(self.send(headers, first, "primera_clave").status_code, 200)
        self.assertEqual(self.send(headers, first, "continúa").status_code, 200)
        first_context = [body for path, body, _ in self.requests if path == "/api/chat"][-1]["messages"]
        self.assertIn("primera_clave", json.dumps(first_context))
        self.assertEqual(self.send(headers, second, "otra conversación").status_code, 200)
        second_context = [body for path, body, _ in self.requests if path == "/api/chat"][-1]["messages"]
        self.assertNotIn("primera_clave", json.dumps(second_context))

    def test_history_survives_application_restart(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.assertEqual(self.send(headers, conv).status_code, 200)
        cookie = self.client.cookies.get(COOKIE_NAME)
        with TestClient(create_app(self.settings, self.transport)) as restarted:
            restarted.cookies.set(COOKIE_NAME, cookie)
            self.assertEqual(len(restarted.get(f"/api/agent/conversations/{conv}/messages").json()), 2)

    def test_unconfigured_provider_returns_actionable_error_without_fake_answer(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.configure_model(model="")
        response = self.send(headers, conv)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "model_not_configured")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/messages").json(), [])

    def test_external_adapter_uses_server_key_and_verifies_chat(self):
        self.configure_model("openai_compatible", output_token_field="max_completion_tokens")
        headers = self.register()
        response = self.client.post("/api/agent/check", headers=headers, json={"backend": "local"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["chat"], "verified")
        self.assertFalse(response.json()["tools"])
        conv = self.conversation(headers)
        self.assertEqual(self.send(headers, conv).status_code, 200)
        self.assertTrue(all(headers.get("authorization") == "Bearer fixture-secret-only-on-server" for _, _, headers in self.requests))
        self.assertIn("max_completion_tokens", self.requests[-1][1])
        public = self.client.get("/api/agent/config").text
        self.assertNotIn("fixture-secret", public)
        self.assertNotIn("base_url", public)
        self.assertNotIn("api_key_env", public)
        self.assertEqual(self.client.get("/.env").status_code, 404)

    def test_provider_temperature_is_optional_and_forwarded_to_each_adapter(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.configure_model(temperature=0)
        self.assertEqual(self.send(headers, conv).status_code, 200)
        self.assertEqual(self.requests[-1][1]["options"]["temperature"], 0)
        self.configure_model("openai_compatible", temperature=.2)
        self.assertEqual(self.send(headers, conv).status_code, 200)
        self.assertEqual(self.requests[-1][1]["temperature"], .2)
        self.configure_model("openai_compatible", temperature=None)
        self.assertEqual(self.send(headers, conv).status_code, 200)
        self.assertNotIn("temperature", self.requests[-1][1])

    def test_tool_probe_timeout_reports_connection_failure_instead_of_missing_capability(self):
        self.configure_model("ollama", verify_tools=True)
        self.mode = "timeout"
        headers = self.register()
        response = self.client.post("/api/agent/check", headers=headers, json={"backend": "local"})
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["code"], "provider_timeout")
        self.assertNotIn("fixture-secret", response.text)

    def test_tool_capability_probe_and_ollama_context_metadata(self):
        self.configure_model("openai_compatible", verify_tools=True)
        self.mode = "probe"
        headers = self.register()
        response = self.client.post("/api/agent/check", headers=headers, json={"backend": "local"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["tools"])
        self.assertEqual(response.json()["tools_evidence"], "prueba de llamada real")
        self.configure_model(context_window=8192)
        response = self.client.post("/api/agent/check", headers=headers, json={"backend": "local"})
        self.assertEqual(response.json()["context_window"], 8192)
        self.assertEqual(response.json()["chat"], "reported")

    def test_provider_errors_are_sanitized_and_turns_are_atomic(self):
        headers = self.register()
        conv = self.conversation(headers)
        for mode, code, status in (("failure", "provider_auth", 503), ("nonjson", "provider_invalid_json", 502), ("timeout", "provider_timeout", 504)):
            with self.subTest(mode=mode):
                self.mode = mode
                response = self.send(headers, conv)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn("fixture-secret", response.text)
                self.assertEqual(self.client.get(f"/api/agent/conversations/{conv}/messages").json(), [])

    def test_idempotent_retry_and_payload_conflict(self):
        headers = self.register()
        conv, request_id = self.conversation(headers), str(uuid.uuid4())
        first = self.send(headers, conv, request_id=request_id)
        count = len(self.requests)
        second = self.send(headers, conv, request_id=request_id)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.requests), count)
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conv}/messages").json()), 2)
        self.assertEqual(self.send(headers, conv, "otro mensaje", request_id).status_code, 409)
        self.configure_model(model="")
        self.assertEqual(self.send(headers, conv, request_id=request_id).json(), first.json())

    def test_own_compatible_server_can_disable_api_key_requirement(self):
        self.configure_model("openai_compatible", require_api_key=False)
        headers = self.register()
        conv = self.conversation(headers)
        with patch.dict(os.environ, {"AGENT_API_KEY": ""}):
            response = self.send(headers, conv)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(all("authorization" not in request_headers for _, _, request_headers in self.requests))

    def test_tools_execute_with_budget_and_deny_unregistered_tools(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.mode = "tools"
        response = self.send(headers, conv)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tools_used"], ["calculator"])
        tool_messages = self.requests[-1][1]["messages"]
        self.assertTrue(any(m["role"] == "tool" and '18' in m["content"] for m in tool_messages))
        self.mode = "forbidden_tool"
        self.assertEqual(self.send(headers, conv).json()["code"], "tool_not_allowed")
        self.mode = "endless"
        response = self.send(headers, conv)
        self.assertEqual(response.json()["code"], "execution_limit")
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conv}/messages").json()), 2)

    def test_memory_retrieval_is_private_and_has_real_document_reference(self):
        headers = self.register()
        document = self.client.post("/api/agent/memory", headers=headers, json={"title": "Mi proyecto", "content": "Mi proyecto usa una clave de color turquesa."}).json()["id"]
        conv = self.conversation(headers)
        response = self.send(headers, conv, "¿Cuál es el color de mi proyecto?")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sources"][0]["document_id"], document)
        self.assertIn("turquesa", json.dumps(self.requests[-1][1]))
        self.client.cookies.clear()
        bob = self.register("bob")
        conv = self.conversation(bob)
        response = self.send(bob, conv, "¿Cuál es el color de mi proyecto?")
        self.assertEqual(response.json()["sources"], [])
        self.assertNotIn("turquesa", json.dumps(self.requests[-1][1]))

    def test_research_uses_search_results_and_does_not_fetch_arbitrary_urls(self):
        headers = self.register()
        conv = self.conversation(headers, "investigador")
        response = self.send(headers, conv, "Busca una referencia primaria")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([s["url"] for s in response.json()["sources"]], ["https://docs.example.org/referencia"])
        self.assertEqual({path for path, _, _ in self.requests}, {"/api/show", "/search", "/api/chat"})
        self.assertEqual(response.json()["tools_used"], ["web_search"])

    def test_research_without_search_configuration_fails_explicitly(self):
        headers = self.register()
        conv = self.conversation(headers, "investigador")
        with patch.dict(os.environ, {"SEARXNG_URL": ""}):
            response = self.send(headers, conv)
        self.assertEqual(response.json()["code"], "search_not_configured")
        self.assertFalse(any(path == "/api/chat" for path, _, _ in self.requests))

    def test_general_can_search_on_model_request_without_project_permissions(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.mode = "web_search"
        response = self.send(headers, conv, "Busca documentación de unittest")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tools_used"], ["web_search"])
        self.assertEqual([s["url"] for s in response.json()["sources"]], ["https://docs.example.org/referencia"])
        calls = [body for path, body, _ in self.requests if path == "/api/chat"]
        names = {tool["function"]["name"] for tool in calls[0]["tools"]}
        self.assertIn("web_search", names)
        self.assertIn("web_read", names)
        self.assertNotIn("sandbox_exec", names)
        self.assertNotIn("project_write", names)
        self.assertTrue(any(m["role"] == "tool" and "docs.example.org/referencia" in m["content"] for m in calls[-1]["messages"]))

    def test_general_chat_does_not_force_search_or_require_search_service(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.mode = "chat_tools"
        with patch.dict(os.environ, {"SEARXNG_URL": ""}):
            response = self.send(headers, conv, "Hola")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tools_used"], [])
        self.assertEqual(response.json()["sources"], [])
        self.assertFalse(any(path == "/search" for path, _, _ in self.requests))

    def test_concurrent_turns_are_rejected_without_context_race(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.mode = "slow"
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(self.send, headers, conv, "primero")
            self.assertTrue(self.entered.wait(3))
            duplicate = self.send(headers, conv, "segundo")
            self.assertEqual(duplicate.status_code, 409, duplicate.text)
            self.assertEqual(running.result(timeout=5).status_code, 200)
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conv}/messages").json()), 2)

    def test_global_deadline_cancels_inference_and_releases_run(self):
        config = self.config()
        config["limits"]["run_timeout_seconds"] = 5
        self.save_config(config)
        headers = self.register()
        conv = self.conversation(headers)
        self.mode = "run_timeout"
        response = self.send(headers, conv)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["code"], "run_timeout")
        self.mode = "text"
        self.assertEqual(self.send(headers, conv).status_code, 200)

    def test_schema_and_prompt_paths_are_validated(self):
        headers = self.register()
        self.assertEqual(self.client.post("/api/agent/conversations", headers=headers, json={"profile": "../../otro"}).status_code, 404)
        profile = json.loads((self.root / "profiles/general.json").read_text("utf-8"))
        profile["prompt"] = "../app.py"
        (self.root / "profiles/general.json").write_text(json.dumps(profile), "utf-8")
        conv = self.conversation(headers)
        self.assertEqual(self.send(headers, conv).json()["code"], "invalid_prompt")

    def test_message_and_memory_limits(self):
        headers = self.register()
        conv = self.conversation(headers)
        self.assertEqual(self.send(headers, conv, "a" * 8001).status_code, 422)
        self.assertEqual(self.client.post("/api/agent/memory", headers=headers, json={"title": "vacío", "content": "  "}).status_code, 422)
        self.assertEqual(self.client.post("/api/messages", headers=headers, json={"message": "", "extra": True}).status_code, 422)


class FoundationTest(unittest.TestCase):
    def test_legacy_migration_backs_up_and_preserves_accounts_and_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as c, c:
                c.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,ip TEXT,created_at TIMESTAMP); CREATE TABLE messages(id INTEGER PRIMARY KEY,username TEXT,message TEXT,timestamp TIMESTAMP);")
                c.execute("INSERT INTO users VALUES(1,'legacy','clave-antigua','ip',NULL)")
                c.execute("INSERT INTO messages VALUES(1,'legacy','conservar',NULL)")
            database = Database(path)
            database.initialize()
            database.initialize()
            self.assertEqual(database.authenticate("legacy", "clave-antigua", False, "ip")["id"], 1)
            self.assertEqual(database.community_messages("legacy")[0]["message"], "conservar")
            backups = list((Path(directory) / ".local/backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as c:
                self.assertEqual(c.execute("SELECT password FROM users").fetchone()[0], "clave-antigua")

    def test_context_trims_complete_pairs_and_rejects_oversized_current_message(self):
        history = [{"role": role, "content": "a" * 500} for _ in range(12) for role in ("user", "assistant")]
        result = build_context("Sistema", history, "Pregunta", [], [], 4096, 1024)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[-1]["content"], "Pregunta")
        self.assertEqual(len(result) % 2, 0)
        self.assertLess(len(result), len(history) + 2)
        with self.assertRaises(AgentError):
            build_context("Sistema", [], "á" * 8000, [], [], 4096, 1024)

    def test_calculator_does_not_execute_code_and_urls_are_restricted(self):
        self.assertEqual(calculate("(2 + 3) * 4 / 2"), 10)
        for value in ("__import__('os').system('echo peligro')", "2 ** 999999", "1e300 * 1e300", "[1,2]"):
            with self.assertRaises((ValueError, SyntaxError)):
                calculate(value)
        for url in ("javascript:alert(1)", "http://127.0.0.1/x", "http://localhost/x", "http://10.0.0.1/x"):
            self.assertFalse(public_source_url(url))
        self.assertTrue(public_source_url("https://docs.example.org/x"))
        with self.assertRaises(ValueError):
            validate_server_url("http://remote.example.org")


if __name__ == "__main__":
    unittest.main()
