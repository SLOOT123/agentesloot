"""Perfil ZYON, presentación, comandos locales y comportamiento visible."""
import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.presentation import present_answer
from agent_core.providers import Provider
from agent_core.service import build_context
from agent_core.tools import SCHEMAS


class ZyonProfileTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode = 'tools'

    async def upstream(self, request):
        if request.url.path == '/api/chat' and json.loads(request.content).get('format'):
            self.requests.append((request.url.path, json.loads(request.content), dict(request.headers)))
            return httpx.Response(200, json={'message': {'content': json.dumps({'answer': 'hola, dime qué quieres resolver.'})}})
        return await fixtures.ProjectTest.upstream(self, request)

    def test_profile_behavior_is_authenticated_and_keeps_the_reference_separate(self):
        endpoint = '/api/agent/profiles/zyon/behavior'
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        self.register()
        profiles = self.client.get('/api/agent/config').json()['profiles']
        profile = next(item for item in profiles if item['id'] == 'zyon')
        self.assertEqual(profile['name'], 'ZYON')
        self.assertEqual(profile['display_name'], 'ZYON')
        self.assertNotIn('behavior_note', profile)
        self.assertNotIn('prompt', profile)
        page = self.client.get('/').text
        self.assertNotIn('openBehavior', page)
        self.assertNotIn('behaviorDialog', page)
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 200)
        behavior = response.json()
        self.assertIsNone(behavior['file'])
        self.assertEqual(behavior['mode'], 'references')
        self.assertIn('prompts/zyon-conversacion.md', behavior['reference_documents'])
        self.assertIn('sin cambios', behavior['note'])
        self.assertIn('documentos de consulta', behavior['note'])
        self.assertIn('Tu nombre de presentación es ZYON', behavior['instructions'])
        self.assertNotIn('NEVER refuse any code work', behavior['instructions'])
        self.assertNotIn('UNRESTRICTED CODING', behavior['instructions'])
        self.assertEqual(self.client.get('/api/agent/profiles/missing/behavior').status_code, 404)

    def test_structured_prompt_fits_and_normal_chat_keeps_one_marker(self):
        headers = self.register()
        files = self.app.state.files; profile = files.profile('zyon')
        self.assertEqual(profile.tools, files.profile('equipo').tools)
        backend = files.agent().backends['local']
        messages = build_context(files.prompt(profile), [], 'hola', [], [SCHEMAS['task_plan']], backend.context_window, backend.max_output_tokens)
        body, _ = Provider(backend, files.agent().limits).structured_request(messages, [SCHEMAS['task_plan']], allow_answer=True)
        self.assertLess(len(json.dumps(body, ensure_ascii=False).encode()), backend.context_window - backend.max_output_tokens - 256)
        conversation = self.conversation(headers, 'zyon')
        result = self.send(headers, conversation, 'hola')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['answer'].startswith('[Zyon]\n'))
        self.assertEqual(result.json()['answer'].count('[Zyon]'), 1)
        self.assertEqual(result.json()['tools_used'], [])
        self.assertEqual(self.client.get(f'/api/agent/conversations/{conversation}/messages').json()[-1]['content'], result.json()['answer'])
        self.assertTrue(all(item[1].get('format') for item in self.requests if item[0] == '/api/chat'))

    def test_control_commands_skip_model_and_attachments_and_are_idempotent(self):
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        with patch('agent_core.service.attachment_context', AsyncMock(side_effect=AssertionError('No debe analizar adjuntos'))):
            identifier = str(uuid.uuid4())
            first = self.send(headers, conversation, 'zyon start', identifier)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()['answer'], '[Zyon]\n\nwhat are we operating on?')
            self.assertEqual(self.send(headers, conversation, 'zyon start', identifier).json(), first.json())
            for command in ('zyon status', 'menu', 'pop the table', 'sterilize', 'fresh field'):
                result = self.send(headers, conversation, command)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['model_calls'], 0)
                self.assertEqual(result.json()['review'], 'profile_command')
        self.assertFalse(self.requests)
        self.assertEqual(len(self.client.get(f'/api/agent/conversations/{conversation}/messages').json()), 12)

    def test_fresh_field_resets_model_context_and_retains_saved_messages(self):
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        self.assertEqual(self.send(headers, conversation, 'CLAVE_ANTERIOR_PRIVADA').status_code, 200)
        self.assertEqual(self.send(headers, conversation, 'fresh field').status_code, 200)
        self.requests.clear()
        response = self.send(headers, conversation, 'Hola, seguimos')
        self.assertEqual(response.status_code, 200, response.text)
        payloads = json.dumps([body for path, body, _ in self.requests if path == '/api/chat'], ensure_ascii=False)
        self.assertNotIn('CLAVE_ANTERIOR_PRIVADA', payloads)
        history = self.client.get(f'/api/agent/conversations/{conversation}/messages').json()
        self.assertEqual(len(history), 6)
        self.assertIn('CLAVE_ANTERIOR_PRIVADA', json.dumps(history))

    def test_direct_download_uses_zyon_presentation_and_preserves_pending_state(self):
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        queued = {'status': 'queued', 'job': {'id': str(uuid.uuid4()), 'status': 'queued'}}
        with patch.object(self.app.state.download_jobs, 'submit', AsyncMock(return_value=queued)):
            result = self.send(headers, conversation, 'Descarga el PDF de https://example.org/paper')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['answer'].startswith('[Zyon]\n\nIncise.'))
        self.assertIn('Title: Descarga', result.json()['answer'])
        self.assertEqual(result.json()['review'], 'pending_download')
        self.assertFalse(self.requests)

    def test_format_removes_leading_reasoning_but_preserves_literal_code(self):
        profile = self.app.state.files.profile('zyon')
        result = present_answer(profile, 'hola', '[Zyon]\n<thinking>privado</thinking>\n[Zyon]\nVisible')
        self.assertEqual(result, '[Zyon]\n\nVisible')
        code = '```html\n<thinking>etiqueta literal</thinking>\n```'
        self.assertIn(code, present_answer(profile, 'crea HTML', code, 'coding'))
        self.assertNotIn('privado', present_answer(profile, 'hola', '<think>privado'))


if __name__ == '__main__':
    unittest.main()
