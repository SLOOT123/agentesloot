"""Descubrimiento local, permisos y acciones de recuperación; sin red ni modelo."""
import asyncio
import unittest

from agent_core.config import AgentError
from agent_core.tool_catalog import discover, recovery_advice
from agent_core.tools import SCHEMAS, Tools


class ToolCatalogTest(unittest.TestCase):
    def test_discovery_never_offers_tools_outside_allowed_capabilities(self):
        descriptions = {name: value['function']['description'] for name, value in SCHEMAS.items()}
        report = discover(['calculator', 'project_read', 'tool_discover'], descriptions, 'calcular matemáticas')
        self.assertEqual({item['name'] for item in report['tools']}, {'calculator', 'project_read'})
        self.assertNotIn('download_url', str(report))
        self.assertIsNone(report['download_tools'])

    def test_missing_dependencies_and_external_access_have_distinct_actions(self):
        inventory = {'tools': [{'name': name, 'available': name != 'ffmpeg'} for name in ('http', 'yt-dlp', 'ffmpeg', 'ffprobe', 'node')]}
        missing = recovery_advice('conversion_failed', 'Falta FFmpeg', inventory)
        self.assertEqual(missing['reason'], 'missing_dependency')
        self.assertEqual(missing['missing_tools'], ['ffmpeg'])
        access = recovery_advice('site_access_required', 'Sign in', inventory)
        self.assertEqual(access['reason'], 'authorized_access_required')
        self.assertNotIn('missing_tools', access)
        self.assertEqual(recovery_advice('site_rate_limited', '', inventory)['status'], 'pending')

    def test_read_only_discovery_is_available_before_a_plan_and_cannot_grant_permissions(self):
        events = []
        tools = Tools(None, 1, 2, task_planning=True, event=lambda *event: events.append(event))
        allowed = ['tool_discover', 'calculator', 'task_plan']
        report = asyncio.run(tools.execute('tool_discover', {'purpose': 'calcular 2+2'}, allowed))
        self.assertIn('calculator', [item['name'] for item in report['tools']])
        self.assertFalse(tools.task_graph.nodes)
        self.assertEqual(events[-1][0], 'tool_discovery')
        with self.assertRaises(AgentError):
            asyncio.run(tools.execute('sandbox_exec', {'language': 'sh', 'code': 'echo test', 'purpose': 'test'}, allowed))

    def test_discovery_in_a_plan_records_observation_normally(self):
        tools = Tools(None, 1, 2, task_planning=True)
        allowed = ['task_plan', 'tool_discover']
        asyncio.run(tools.execute('task_plan', {'steps': [{'id': 'discover', 'title': 'Detectar herramientas', 'tool': 'tool_discover', 'depends_on': []}]}, allowed))
        asyncio.run(tools.execute('tool_discover', {'purpose': 'detectar capacidades'}, allowed))
        self.assertTrue(tools.task_graph.snapshot()['complete'])
