"""Una apertura de proyecto debe pedir el objetivo sin inventar acciones."""
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import test_project as fixtures

from agent_core.projects import ProjectFiles
from agent_core.service import conversational_followup, download_requested, project_goal_missing
from test_attachments import pdf


class ProjectIntroductionTest(unittest.TestCase):
    def test_only_bare_project_introductions_need_a_goal(self):
        for message in ('vamos a trabaja en unproyecto', 'Vamos a trabajar en un proyecto.',
                        'Quiero crear una app', 'Crea un proyecto', 'Empecemos con un proyecto nuevo',
                        'necesito un programa', 'Me gustaría hacer una aplicación',
                        'quiero crea  proyectos de siberseguridad', 'Quiero crear proyectos de ciberseguridad',
                        'Quiero crear un proyecto de ciberseguridad', 'Quiero hacer proyectos con Python'):
            with self.subTest(message=message):
                self.assertTrue(project_goal_missing(message))
        for message in ('Crea un proyecto de una calculadora', 'Vamos a trabajar en un proyecto de inventario',
                        'Escribe y ejecuta un programa Python que imprima 4', 'ejecútalo', 'build it',
                        'crea un proyecto https://example.org/requisitos', 'Resume el PDF', 'hola',
                        'Quiero crear una app\nprint(4)', 'Crea proyectos que validen archivos con Python',
                        'Crea proyectos de ciberseguridad y ejecútalos'):
            with self.subTest(message=message):
                self.assertFalse(project_goal_missing(message))


class ConversationRoutingTest(unittest.TestCase):
    def test_examples_and_explanations_are_distinct_from_actions(self):
        for message in ('DAME UN EJEMPLO SOBRE ESE TEMA', 'Dame otro ejemplo',
                        'Explícame eso con más detalle', '¿Qué significa cifrado?',
                        'Por favor dame un ejemplo sencillo', 'Resume lo anterior'):
            with self.subTest(message=message):
                self.assertTrue(conversational_followup(message))
                self.assertFalse(download_requested(message))
        for message in ('Dame un ejemplo y ejecútalo', 'Dame un ejemplo y guárdalo en ejemplo.py',
                        'Dame un ejemplo y pruébalo', 'Explícame la página 4 del PDF',
                        'Resume el archivo adjunto', 'Busca un ejemplo en internet',
                        'Dame un ejemplo y crea un archivo', 'Calcula 12 * 4',
                        'Descarga ese enlace', 'Explica https://example.org/tema'):
            with self.subTest(message=message):
                self.assertFalse(conversational_followup(message))
        for message in ('Descarga ese enlace', 'Quiero descargar una cumbia',
                        'Busca y descarga el PDF', 'Extrae el audio en MP3', 'Bájame ese vídeo'):
            with self.subTest(message=message):
                self.assertTrue(download_requested(message))
        self.assertFalse(download_requested('No quiero descargar nada'))


class ContextualExampleTest(unittest.TestCase):
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
        self.answer = 'El tema es el cifrado: una clave permite recuperar el texto original.'
        config = self.config()
        config['multi_agent']['enabled'] = True
        self.save_config(config)

    async def upstream(self, request):
        if request.url.path != '/api/chat':
            body = json.loads(request.content) if request.content else {}
            if request.url.path == '/api/show' and body.get('model') == 'qwen3-vl:2b-instruct':
                return httpx.Response(200, json={'capabilities': ['completion', 'vision']})
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        content = 'Una página sobre cifrado.' if body.get('model') == 'qwen3-vl:2b-instruct' else self.answer
        if body.get('format'):
            content = json.dumps({'answer': content})
        return httpx.Response(200, json={'message': {'content': content}})

    def upload_pdf(self, headers, conversation):
        result = self.client.post(f'/api/agent/conversations/{conversation}/attachments', headers=headers,
                                  files={'file': ('informe.pdf', pdf(scan=True), 'application/pdf')})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def assert_no_actions(self, result):
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['tools_used'], [])
        self.assertNotIn('download_job', result.json())
        calls = [body for path, body, _ in self.requests if path == '/api/chat']
        self.assertTrue(calls)
        self.assertTrue(all(not call.get('tools') for call in calls))
        for call in calls:
            if call.get('format'):
                self.assertEqual(call['format']['anyOf'][0]['required'], ['answer'])
                self.assertEqual(len(call['format']['anyOf']), 1)

    def test_example_uses_previous_pdf_analysis_without_redownloading_or_reanalyzing(self):
        headers = self.register()
        conversation = self.conversation(headers)
        uploaded = self.upload_pdf(headers, conversation)
        analysis = self.send(headers, conversation, 'Analiza el PDF')
        self.assertEqual(analysis.status_code, 200, analysis.text)
        self.assertEqual(analysis.json()['attachments_used'], [uploaded['id']])
        self.requests.clear()
        self.answer = 'Por ejemplo, Ana cifra un mensaje y Luis lo recupera con la clave.'
        with patch('agent_core.service.attachment_context', AsyncMock(side_effect=AssertionError('Análisis repetido'))), \
                patch('agent_core.service.TeamCouncil') as council, \
                patch('agent_core.team_research.TeamResearch') as research, \
                patch('agent_core.tools.Tools.execute', AsyncMock(side_effect=AssertionError('Acción no solicitada'))):
            result = self.send(headers, conversation, 'DAME UN EJEMPLO SOBRE ESE TEMA')
        self.assert_no_actions(result)
        self.assertEqual(result.json()['answer'], self.answer)
        self.assertEqual(result.json()['attachments_used'], [])
        council.assert_not_called()
        research.assert_not_called()
        calls = [body for path, body, _ in self.requests if path == '/api/chat']
        self.assertTrue(any(message['content'] == analysis.json()['answer']
                            for message in calls[0]['messages'] if message['role'] == 'assistant'))
        activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
        self.assertEqual(activity['status'], 'complete')
        self.assertFalse(any(event['kind'] in {'tool', 'attachment_vision', 'worker_started'} for event in activity['events']))

    def test_a_new_pdf_is_still_analyzed_even_when_requesting_an_example(self):
        headers = self.register()
        conversation = self.conversation(headers)
        uploaded = self.upload_pdf(headers, conversation)
        result = self.send(headers, conversation, 'Dame un ejemplo sobre ese tema')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['attachments_used'], [uploaded['id']])
        calls = [body for path, body, _ in self.requests if path == '/api/chat']
        self.assertTrue(any(call.get('model') == 'qwen3-vl:2b-instruct' for call in calls))
        for call in calls:
            self.assertNotIn('download_url', [tool['function']['name'] for tool in call.get('tools', [])])

    def test_greetings_do_not_start_research_in_any_profile(self):
        headers = self.register()
        with patch('agent_core.service.TeamCouncil') as council, \
                patch('agent_core.team_research.TeamResearch') as research:
            for profile in ('general', 'investigador', 'zyon'):
                for enabled in (False, True):
                    with self.subTest(profile=profile, multi_agent=enabled):
                        config = self.config()
                        config['multi_agent']['enabled'] = enabled
                        self.save_config(config)
                        self.requests.clear()
                        conversation = self.conversation(headers, profile)
                        result = self.send(headers, conversation, 'HOLA')
                        self.assert_no_actions(result)
                        self.assertNotIn('/search', [path for path, _, _ in self.requests])
        council.assert_not_called()
        research.assert_not_called()

    def test_explanation_does_not_force_checks_of_old_project_files(self):
        headers = self.register()
        conversation = self.conversation(headers, 'zyon')
        project = ProjectFiles(self.app.state.database, 1, conversation)
        project.write([{'path': 'anterior.py', 'content': 'print(4)\n'}])
        original = project.read()
        result = self.send(headers, conversation, 'Explícame qué significa cifrado')
        self.assert_no_actions(result)
        self.assertTrue(result.json()['project_unverified'])
        self.assertEqual(result.json()['review'], 'model_reviewed')
        self.assertNotIn('pendiente', result.json()['answer'])
        self.assertEqual(project.read(), original)


class ProjectClarificationTest(unittest.TestCase):
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
        self.meta_answer = False
        config = self.config()
        config['multi_agent']['enabled'] = True
        self.save_config(config)

    async def upstream(self, request):
        if request.url.path != '/api/chat':
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        # El modelo solo debe recibir el contrato de respuesta, jamás uno de acciones.
        self.assertNotIn('tools', body)
        self.assertEqual(body['format']['anyOf'], [{
            'type': 'object', 'properties': {'answer': {'type': 'string'}},
            'required': ['answer'], 'additionalProperties': False}])
        return httpx.Response(200, json={'message': {'content': json.dumps({
            'answer': 'Pregunta por el objetivo del proyecto' if self.meta_answer else 'Claro. ¿Qué quieres que haga el proyecto?'})}})

    def test_project_intro_asks_instead_of_planning_or_running_commands(self):
        headers = self.register()
        with patch('agent_core.tools.SandboxRunner') as runner, patch('agent_core.service.TeamCouncil') as council:
            runner.return_value.run = AsyncMock()
            for message in ('vamos a trabaja en unproyecto', 'Crea un proyecto', 'quiero crea  proyectos de siberseguridad'):
                with self.subTest(message=message):
                    conversation = self.conversation(headers, 'zyon')
                    response = self.send(headers, conversation, message)
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    self.assertEqual(result['answer'], '[Zyon]\n\nClaro. ¿Qué quieres que haga el proyecto?')
                    self.assertEqual(result['tools_used'], [])
                    self.assertEqual(result['model_calls'], 2)
                    self.assertEqual(result['review'], 'model_reviewed')
                    self.assertIsNone(result['task_graph'])
                    self.assertEqual(self.client.get(f'/api/agent/conversations/{conversation}/project').json()['files'], [])
                    activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
                    self.assertEqual(activity['status'], 'complete')
                    self.assertTrue(any(event['kind'] == 'clarification' for event in activity['events']))
                    self.assertFalse(any(event['kind'] in {'tool', 'worker_started', 'sandbox_started'} for event in activity['events']))
            runner.return_value.run.assert_not_awaited()
            council.assert_not_called()

    def test_old_unverified_files_do_not_force_actions_during_clarification(self):
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        project = ProjectFiles(self.app.state.database, 1, conversation)
        project.write([{'path': 'anterior.py', 'content': 'print(4)\n'}])
        original = project.read()
        response = self.send(headers, conversation, 'vamos a trabajar en un proyecto')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['tools_used'], [])
        self.assertEqual(response.json()['review'], 'model_reviewed')
        self.assertTrue(response.json()['project_unverified'], 'Los archivos previos no se verifican por conversar')
        self.assertNotIn('pendiente', response.json()['answer'])
        self.assertEqual(project.read(), original)
        payloads = json.dumps([body for path, body, _ in self.requests if path == '/api/chat'], ensure_ascii=False)
        self.assertNotIn('Completa las comprobaciones permitidas', payloads)

    def test_model_instruction_is_replaced_with_a_user_question(self):
        self.meta_answer = True
        headers = self.register(); conversation = self.conversation(headers, 'zyon')
        response = self.send(headers, conversation, 'vamos a trabaja en unproyecto')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['answer'], '[Zyon]\n\n¿Qué quieres que haga el proyecto y qué resultado necesitas primero?')
        self.assertEqual(result['review'], 'clarification', 'La pregunta del servidor no debe atribuirse a una revisión del modelo')
        self.assertEqual(result['tools_used'], [])
        activity = self.client.get(f'/api/agent/conversations/{conversation}/activity').json()
        self.assertTrue(any(event['payload'].get('reason') == 'model_returned_instruction' for event in activity['events']))


if __name__ == '__main__':
    unittest.main()
