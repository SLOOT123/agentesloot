"""Compatibilidad observada con Llama: JSON codificado en strings, sin relajar permisos ni tipos de código."""
import json
import unittest

from agent_core.providers import normalize_turn
from agent_core.tools import SCHEMAS


class ModelArgumentsTest(unittest.TestCase):
    def call(self, name, arguments):
        turn = normalize_turn({"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}, [SCHEMAS[name]])
        return json.loads(turn.calls[0]["function"]["arguments"])

    def test_decodes_only_json_types_declared_in_tool_schema(self):
        files = [{"path": "saludo.py", "content": "123"}]
        arguments = self.call("project_write", {"files": json.dumps(files)})
        self.assertEqual(arguments["files"], files)
        self.assertIsInstance(arguments["files"][0]["content"], str)
        script = self.call("sandbox_exec", {"language": "python", "code": "true", "purpose": "Prueba", "project": "true"})
        self.assertIs(script["project"], True)
        self.assertEqual(script["code"], "true")
        self.assertEqual(self.call("project_read", {"path": "", "offset": "1000"})["offset"], 1000)

    def test_invalid_types_code_and_unknown_fields_are_not_coerced(self):
        for value in ("1", "yes", "__import__('os').getcwd()", "null"):
            arguments = self.call("sandbox_exec", {"project": value})
            self.assertEqual(arguments["project"], value)
        self.assertEqual(self.call("project_write", {"files": "{}"})["files"], "{}")
        arguments = self.call("project_write", {"files": "[]", "extra": "true"})
        self.assertEqual(arguments["files"], [])
        self.assertEqual(arguments["extra"], "true")
