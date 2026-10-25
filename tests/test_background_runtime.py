"""Procesos Python reales con protocolo fijo de prueba; sin HTTP ni modelo de IA."""
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_core.downloads import Downloader
from agent_core.retry_policy import backoff_delay
from agent_core.storage import Database
from scripts.monitor_agent import snapshot


class BackoffModelTest(unittest.TestCase):
    def test_exponential_cap_and_server_minimum(self):
        self.assertEqual([backoff_delay(i) for i in range(1, 7)], [300, 600, 1200, 2400, 3600, 3600])
        self.assertEqual(backoff_delay(1000000), 3600)
        self.assertEqual(backoff_delay(1, 7200), 7200)
        for value in ('invalid', float('inf'), float('nan'), -5):
            self.assertEqual(backoff_delay(1, value), 300)

    def test_monitor_does_not_create_missing_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                snapshot(path)
            self.assertFalse(path.exists())

    def test_vector_memory_is_scoped_to_owner_and_normalizes_accents(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / 'test.sqlite3')
            database.initialize()
            with database.connection() as connection:
                connection.executemany("INSERT INTO users(id,username,password) VALUES(?,?,?)", [(1, 'owner', 'unused'), (2, 'other', 'unused')])
            database.add_document(1, 'Mi nota', 'Información sobre café colombiano')
            database.add_document(2, 'SECRETO', 'Café café café café')
            rows = database.search_documents(1, 'cafe')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['title'], 'Mi nota')
            self.assertEqual(rows[0]['ranking']['method'], 'tfidf_cosine_v1')
            self.assertEqual(database.search_documents(1, 'desconocido'), [])


class WorkerRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.events = []
        self.started = asyncio.Event()
        self.store = SimpleNamespace(root=self.directory, save=Mock(return_value={'filename': 'fixture.mp3'}))
        self.downloader = Downloader(self.store, event=self.observe)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def observe(self, kind, title, payload):
        self.events.append(payload)
        if payload.get('kind') == 'process_started':
            self.started.set()

    def fixed_worker(self, code):
        # Se sustituye el trabajador de red por código fijo; se conserva el proceso real.
        original = asyncio.create_subprocess_exec
        async def launch(*command, **kwargs):
            if command[0] == sys.executable:
                return await original(sys.executable, '-I', '-u', '-c', code, **kwargs)
            return await original(*command, **kwargs)
        return patch('agent_core.downloads.asyncio.create_subprocess_exec', side_effect=launch)

    async def test_real_worker_pid_exit_and_elapsed_are_observed(self):
        code = "import json,sys; json.load(sys.stdin); print(json.dumps({'kind':'result','ok':False,'error':'fixture_error'}), flush=True)"
        with self.fixed_worker(code):
            result = await self.downloader.run('https://example.org/file')
        self.assertEqual(result['error'], 'fixture_error')
        started, finished = self.events[0], self.events[-1]
        self.assertEqual(started['kind'], 'process_started')
        self.assertGreater(started['pid'], 0)
        self.assertEqual(finished['pid'], started['pid'])
        self.assertEqual(finished['kind'], 'process_finished')
        self.assertEqual(finished['exit_code'], 0)
        self.assertGreaterEqual(finished['elapsed_seconds'], 0)
        self.store.save.assert_not_called()

    async def test_success_protocol_with_nonzero_exit_never_saves_a_file(self):
        code = "import json,sys; json.load(sys.stdin); print(json.dumps({'kind':'result','ok':True}), flush=True); sys.exit(9)"
        with self.fixed_worker(code):
            result = await self.downloader.run('https://example.org/file')
        self.assertFalse(result['ok'])
        self.assertEqual(self.events[-1]['exit_code'], 9)
        self.store.save.assert_not_called()

    async def test_cancellation_stops_real_worker_and_records_closure(self):
        code = 'import time; time.sleep(30)'
        with self.fixed_worker(code):
            task = asyncio.create_task(self.downloader.run('https://example.org/file'))
            await asyncio.wait_for(self.started.wait(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        self.assertEqual(self.events[-1]['kind'], 'process_finished')
        self.assertEqual(self.events[-1]['status'], 'cancelled')
        self.assertIsNotNone(self.events[-1]['exit_code'])
        self.store.save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
