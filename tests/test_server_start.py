"""Arranque único, identidad de la instancia y errores de puerto sin detener procesos."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from agent_core.server_start import SERVICE_NAME, browser_url, instance_key, is_running, serve


class ServerStartTest(unittest.TestCase):
    def test_health_requires_this_application_and_this_workspace(self):
        for payload, expected in [
            ({'service': SERVICE_NAME, 'instance': 'own', 'status': 'ok'}, True),
            ({'service': SERVICE_NAME, 'instance': 'other', 'status': 'ok'}, False),
            ({'service': 'other-server', 'instance': 'own', 'status': 'ok'}, False),
            ({'service': SERVICE_NAME, 'instance': 'own', 'status': 'starting'}, False),
            ({'padding': 'x' * 1024}, False),
        ]:
            client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
            with self.subTest(expected=expected, payload=str(payload)[:100]), patch('agent_core.server_start.httpx.Client', return_value=client):
                self.assertEqual(is_running('http://127.0.0.1:8000', 'own'), expected)

    def test_duplicate_run_reports_existing_server_without_starting_another(self):
        app = SimpleNamespace(state=SimpleNamespace(instance_key='own'))
        with patch('agent_core.server_start.is_running', return_value=True), \
             patch('agent_core.server_start.uvicorn.Server') as server, \
             patch('agent_core.server_start.socket.socket') as listener, redirect_stdout(io.StringIO()) as output:
            result = serve(app)
        self.assertEqual(result, 0)
        server.assert_not_called(); listener.assert_not_called()
        self.assertIn('ya está funcionando', output.getvalue())
        self.assertIn('http://127.0.0.1:8000', output.getvalue())

    def test_occupied_port_never_starts_another_application(self):
        app = SimpleNamespace(state=SimpleNamespace(instance_key='own'))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            port = occupied.getsockname()[1]
            with patch('agent_core.server_start.is_running', return_value=False), \
                 patch('agent_core.server_start.time.sleep'), patch('agent_core.server_start.uvicorn.Server') as server, \
                 redirect_stdout(io.StringIO()) as output:
                result = serve(app, port=port)
        self.assertEqual(result, 1)
        server.assert_not_called()
        self.assertIn(str(port), output.getvalue())
        self.assertIn('No se detuvo ningún proceso', output.getvalue())

    def test_available_port_is_bound_once_and_passed_to_uvicorn(self):
        app = SimpleNamespace(state=SimpleNamespace(instance_key='own'))
        observed = []
        def run(*, sockets):
            self.assertEqual(len(sockets), 1)
            observed.append(sockets[0].getsockname())
        with patch('agent_core.server_start.is_running', return_value=False), \
             patch('agent_core.server_start.uvicorn.Server') as server, redirect_stdout(io.StringIO()):
            server.return_value.run.side_effect = run
            self.assertEqual(serve(app, port=0), 0)
        self.assertEqual(observed[0][0], '127.0.0.1')
        self.assertGreater(observed[0][1], 0)

    def test_browser_address_and_database_identity(self):
        self.assertEqual(browser_url('0.0.0.0', 8000), 'http://127.0.0.1:8000')
        self.assertEqual(browser_url('::', 8000), 'http://[::1]:8000')
        self.assertNotEqual(instance_key(Path('.'), Path('one.db')), instance_key(Path('.'), Path('two.db')))


if __name__ == '__main__':
    unittest.main()
