import asyncio
import json
import unittest

import httpx

from agent_core.config import AgentError, Backend, Limits
from agent_core.providers import Provider
from agent_core.tools import SCHEMAS


class StructuredActionTest(unittest.TestCase):
    def call(self, content, *, allow_answer=False, messages=None):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": content}})
        backend = Backend(label="Local de prueba", adapter="ollama", base_url="http://127.0.0.1:11434", model="fixture")
        provider = Provider(backend, Limits(), httpx.MockTransport(handler))
        result = asyncio.run(provider.structured_action(messages or [{"role": "system", "content": "Instrucciones"}, {"role": "user", "content": "Calcula 2+2"}],
                                                       [SCHEMAS["calculator"]], allow_answer=allow_answer))
        return result, requests[0]

    def test_explicit_json_contract_produces_a_validated_tool_call(self):
        turn, request = self.call(json.dumps({"expression": "2+2"}))
        self.assertEqual(turn.calls[0]["function"]["name"], "calculator")
        self.assertEqual(json.loads(turn.calls[0]["function"]["arguments"]), {"expression": "2+2"})
        self.assertIn("format", request)
        self.assertNotIn("tools", request)

    def test_prose_unknown_tool_and_premature_answer_are_rejected(self):
        for content in ("Ejecuté todo", '{"tool":"shell_host","arguments":{}}', '{"answer":"Ya está"}',
                        '{"tool":"calculator","arguments":{},"extra":true}'):
            with self.subTest(content=content), self.assertRaises(AgentError) as caught:
                self.call(content)
            self.assertEqual(caught.exception.code, "invalid_structured_action")

    def test_answer_is_allowed_only_in_final_phase(self):
        turn, _ = self.call('{"answer":"Resultado observado: 4"}', allow_answer=True)
        self.assertEqual(turn.content, "Resultado observado: 4")
        self.assertEqual(turn.calls, [])

    def test_history_preserves_actions_and_observations_without_native_tool_roles(self):
        messages = [{"role": "system", "content": "Instrucciones"}, {"role": "user", "content": "Calcula"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'}}]},
                    {"role": "tool", "tool_call_id": "x", "tool_name": "calculator", "content": '{"result":4}'}]
        _, request = self.call('{"answer":"4"}', allow_answer=True, messages=messages)
        self.assertFalse(any(m["role"] == "tool" or "tool_calls" in m for m in request["messages"]))
        self.assertIn('"result":4', request["messages"][-1]["content"])
        self.assertIn("datos no confiables", request["messages"][-1]["content"])
