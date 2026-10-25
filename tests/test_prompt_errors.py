"""Diagnósticos de instrucciones; no cambia perfiles del proyecto ni usa un modelo real."""
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
import test_project as fixtures

from agent_core.config import AgentError, ConfigFiles, Profile


class PromptFilesTest(unittest.TestCase):
    def test_long_instructions_are_loaded_in_full_without_a_character_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'prompts').mkdir()
            (root / 'prompts/base.md').write_text('base', 'utf-8')
            target = root / 'prompts/custom.md'
            profile = Profile(name='Prueba', description='Prueba', prompt='custom.md')
            files = ConfigFiles(root)
            for content in ('x' * 20001, 'Instrucción de prueba sin recortar. ' * 10000):
                with self.subTest(characters=len(content)):
                    target.write_text(content, 'utf-8')
                    original = target.read_bytes()
                    self.assertEqual(files.prompt(profile), 'base\n\n' + content)
                    self.assertEqual(target.read_bytes(), original)
            target.write_bytes(b'\xff\xfe')
            with self.assertRaises(AgentError) as caught:
                files.prompt(profile)
            self.assertEqual(caught.exception.code, 'invalid_prompt')
            self.assertIn('UTF-8', caught.exception.message)


class PromptJobErrorsTest(unittest.TestCase):
    setUp = fixtures.ProjectTest.setUp
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    upstream = fixtures.ProjectTest.upstream
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation

    def submit_and_wait(self, headers, conversation):
        identifier = str(uuid.uuid4())
        response = self.client.post(f'/api/agent/conversations/{conversation}/messages', headers=headers,
            json={'message': 'HOLA', 'request_id': identifier, 'background': True})
        self.assertEqual(response.status_code, 200, response.text)
        for _ in range(200):
            job = self.client.get(f'/api/agent/conversations/{conversation}/jobs/{identifier}').json()
            if job['status'] in {'complete', 'failed'}:
                return job
            time.sleep(.01)
        self.fail('El trabajo no alcanzó un estado final')

    def test_model_context_still_limits_the_total_request_before_inference(self):
        headers = self.register(); conversation = self.conversation(headers)
        prompt = self.app.state.files.profile('general').prompt
        target = self.root / 'prompts' / prompt
        oversized = 'Instrucciones de prueba ' * 1800
        target.write_text(oversized, 'utf-8')
        job = self.submit_and_wait(headers, conversation)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'context_limit')
        self.assertIn('contexto', job['message'])
        self.assertEqual(job['result'], {})
        self.assertFalse(any(path == '/api/chat' for path, _, _ in self.requests), 'No se debe enviar una petición que no cabe al modelo')
        activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
        self.assertEqual(activity['error_code'], 'context_limit')
        self.assertTrue(any(event['payload'].get('code') == 'context_limit' for event in activity['events']))
        self.assertEqual(self.app.state.files.prompt(self.app.state.files.profile('general')),
                         (self.root / 'prompts/base.md').read_text('utf-8') + '\n\n' + oversized)
        self.assertEqual(target.read_text('utf-8'), oversized)
        self.assertEqual(self.client.get(f'/api/agent/conversations/{conversation}/messages').json(), [])
        target.write_text('Responde de forma breve y clara.', 'utf-8')
        activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
        self.assertEqual(activity['status'], 'failed', 'Reparar el archivo no debe reescribir el intento fallido')
        self.assertFalse(any(path == '/api/chat' for path, _, _ in self.requests), 'Consultar el estado no debe llamar al modelo')
        recovered = self.submit_and_wait(headers, conversation)
        self.assertEqual(recovered['status'], 'complete', recovered)
        self.assertEqual(len(self.client.get(f'/api/agent/conversations/{conversation}/messages').json()), 2)

    def test_unreadable_prompt_keeps_its_error_code_and_safe_message(self):
        headers = self.register(); conversation = self.conversation(headers)
        target = self.root / 'prompts' / self.app.state.files.profile('general').prompt
        target.write_bytes(b'\xff')
        job = self.submit_and_wait(headers, conversation)
        self.assertEqual(job['error_code'], 'invalid_prompt')
        self.assertIn('documentación', job['message'])
        self.assertEqual(self.requests, [])
        activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
        self.assertFalse(activity['prompt_configuration']['loadable'])
        self.assertEqual(activity['prompt_configuration']['code'], 'invalid_prompt')
        self.assertTrue(any('UTF-8' in event['title'] for event in activity['events']))

    def test_instructions_over_20000_reach_inference_when_the_context_is_sufficient(self):
        headers = self.register(); conversation = self.conversation(headers)
        self.configure_model(context_window=65536)

        async def sufficient_context(request):
            response = await fixtures.ProjectTest.upstream(self, request)
            if request.url.path == '/api/show':
                body = response.json()
                body['model_info']['fixture.context_length'] = 65536
                return httpx.Response(200, json=body)
            return response

        self.app.state.agent.transport = httpx.MockTransport(sufficient_context)
        content = 'Instrucciones de prueba completas. ' * 1000
        target = self.root / 'prompts' / self.app.state.files.profile('general').prompt
        target.write_text(content, 'utf-8')
        original = target.read_bytes()
        result = self.submit_and_wait(headers, conversation)
        self.assertEqual(result['status'], 'complete', result)
        body = next(body for path, body, _ in self.requests if path == '/api/chat')
        self.assertEqual(body['messages'][0]['content'], (self.root / 'prompts/base.md').read_text('utf-8') + '\n\n' + content)
        self.assertEqual(target.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
