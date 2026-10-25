"""Enlaces JSON a MD inmutables, sin concatenación ni activación de su contenido."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx
import test_project as fixtures

from agent_core.config import AgentError
from agent_core.reference_documents import ReferenceDocuments
from agent_core.tools import Tools


class ReferenceDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs').mkdir()
        self.text = 'Contexto de ejemplo.\n' * 2500 + '\nUbicación del color_tema_azul: #0088ff.\n'
        self.paths = ['docs/uno.md', 'docs/dos.md']
        for name in self.paths:
            (self.root / name).write_text(self.text, 'utf-8')
        self.before = {name: (self.root / name).read_bytes() for name in self.paths}
        self.library = ReferenceDocuments(self.root, self.paths)

    def test_duplicate_links_share_one_document_and_can_be_read_in_full(self):
        self.assertEqual(len(self.library.catalog()), 1)
        entry = self.library.catalog()[0]
        self.assertEqual(entry['aliases'], self.paths)
        collected, offset = '', 0
        while True:
            result = self.library.read(entry['reference_id'], offset)
            self.assertLessEqual(len(result['text']), 1000)
            collected += result['text']
            if result['next_offset'] is None:
                break
            offset = result['next_offset']
        self.assertEqual(collected, self.before[self.paths[0]].decode('utf-8'))
        for name, original in self.before.items():
            self.assertEqual((self.root / name).read_bytes(), original)

    def test_relevant_tail_is_retrieved_with_a_bounded_budget(self):
        found = self.library.sources('Ubicación del color_tema_azul', budget=600)
        self.assertEqual(len(found), 1)
        self.assertIn('#0088ff', found[0]['excerpt'])
        self.assertLessEqual(len(found[0]['excerpt']), 600)
        self.assertGreater(found[0]['offset'], 30000)
        self.assertEqual(self.library.sources('hola'), [])
        self.assertEqual(self.library.sources('color_tema_azul', budget=0), [])

    def test_paths_and_identifiers_cannot_read_other_files(self):
        for name in ('../externo.md', str(self.root.parent / 'externo.md'), 'docs/secreto.txt', 'otro.md'):
            with self.subTest(path=name), self.assertRaises(AgentError):
                ReferenceDocuments(self.root, [name])
        for identifier, offset in (('../secreto', 0), ('no_configurado', 0), ('', -1), ('', True)):
            with self.subTest(identifier=identifier, offset=offset), self.assertRaises(AgentError):
                self.library.read(identifier, offset)

    def test_tool_only_reads_configured_documents_and_records_distinct_sources(self):
        (self.root / 'docs/tres.md').write_text('Otro documento distinto.', 'utf-8')
        library = ReferenceDocuments(self.root, [*self.paths, 'docs/tres.md'])
        tool = Tools(None, 1, 2)
        tool.references = library
        async def exercise():
            catalog = await tool.execute('reference_read', {'reference_id': ''}, ['reference_read'])
            self.assertEqual(len(catalog['files']), 2)
            for entry in catalog['files']:
                result = await tool.execute('reference_read', {'reference_id': entry['reference_id']}, ['reference_read'])
                self.assertIsNotNone(result['source_id'])
            self.assertEqual(len(tool.sources), 2)
            with self.assertRaises(AgentError):
                await tool.execute('reference_read', {'reference_id': 'ajeno'}, ['reference_read'])
            with self.assertRaises(AgentError):
                await tool.execute('reference_read', {'reference_id': '', 'path': '../secreto.md'}, ['reference_read'])
        asyncio.run(exercise())


class ReferenceProfileTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode = 'tools'; self.steps = 0; self.read_action = False
        self.reference_text = 'Contenido de referencia.\n' * 2000 + '\ncolor_tema_azul = #0088ff\n'
        for name in ('uno.md', 'dos.md'):
            (self.root / 'docs' / name).write_text(self.reference_text, 'utf-8')
        path = self.root / 'profiles/zyon.json'
        profile = json.loads(path.read_text('utf-8'))
        profile.update(prompt_mode='references', reference_documents=['docs/uno.md', 'docs/dos.md'])
        path.write_text(json.dumps(profile), 'utf-8')
        self.identifier = hashlib.sha256((self.root / 'docs/uno.md').read_bytes()).hexdigest()

    async def upstream(self, request):
        if request.url.path != '/api/chat':
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        self.steps += 1
        answer = {'answer': 'Resultado obtenido de los fragmentos disponibles.'}
        if self.read_action and self.steps == 1:
            answer = {'tool': 'task_plan', 'arguments': {'steps': [{'id': 'leer', 'title': 'Leer un fragmento del documento', 'tool': 'reference_read', 'depends_on': []}]}}
        elif self.read_action and self.steps == 2:
            answer = {'reference_id': self.identifier, 'offset': 1000}
        return httpx.Response(200, json={'message': {'content': json.dumps(answer)}})

    def test_greeting_fits_even_with_large_duplicated_markdown_and_preserves_files(self):
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        before = (self.root / 'docs/uno.md').read_bytes()
        response = self.send(headers, conversation, 'hola')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['tools_used'], [])
        self.assertEqual(response.json()['sources'], [])
        for path, body, _ in self.requests:
            if path == '/api/chat':
                self.assertNotIn('color_tema_azul', json.dumps(body, ensure_ascii=False))
                self.assertEqual(body['format']['anyOf'][0]['required'], ['answer'])
        self.assertEqual((self.root / 'docs/uno.md').read_bytes(), before)
        activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
        entry = next(event for event in activity['events'] if event['kind'] == 'reference_documents')
        self.assertEqual(entry['payload'], {'configured_paths': 2, 'unique_documents': 1})

    def test_question_receives_the_relevant_fragment_and_can_read_more(self):
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        response = self.send(headers, conversation, '¿Dónde está color_tema_azul?')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('#0088ff', response.json()['sources'][0]['excerpt'])
        self.assertEqual(response.json()['sources'][0]['kind'], 'profile_reference')
        self.read_action = True; self.steps = 0
        response = self.send(headers, conversation, 'Lee otro fragmento del MD')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['tools_used'], ['task_plan', 'reference_read'])
        self.assertTrue(response.json()['task_graph']['complete'])


if __name__ == '__main__':
    unittest.main()
