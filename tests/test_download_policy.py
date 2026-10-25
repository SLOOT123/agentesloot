"""Cola/pausas reales en SQLite; proveedor de descarga simulado, sin red."""
import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from agent_core.config import DownloadConfig
from agent_core.download_jobs import DownloadJobs
from agent_core import download_worker as worker
from agent_core.storage import Database


class RetryAfterTest(unittest.TestCase):
    def test_seconds_dates_and_wrapped_errors(self):
        for value, expected in [('90', 90), ('0', 0), ('-10', None), ('invalid', None)]:
            error = HTTPError('https://example.org/', 429, 'Too Many Requests', {'Retry-After': value}, None)
            self.assertEqual(worker.retry_after_seconds(error), expected)
        date = format_datetime(datetime.fromtimestamp(1800000120, timezone.utc), usegmt=True)
        error = HTTPError('https://example.org/', 429, 'Too Many Requests', {'Retry-After': date}, None)
        wrapped = SimpleNamespace(exc_info=(type(error), error, None))
        with patch.object(worker.time, 'time', return_value=1800000000):
            self.assertEqual(worker.retry_after_seconds(wrapped), 120)
        cycle = SimpleNamespace(headers={})
        cycle.cause = cycle
        self.assertIsNone(worker.retry_after_seconds(cycle))

    def test_http_429_preserves_retry_after_and_does_not_try_another_extractor(self):
        emitted = []
        error = HTTPError('https://example.org/', 429, 'Too Many Requests', {'Retry-After': '600'}, None)
        with tempfile.TemporaryDirectory() as temp, patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
             patch.object(worker.Path, 'cwd', return_value=Path(temp)), patch.object(worker, 'direct', side_effect=error), \
             patch.object(worker, 'media_download') as media, patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
            worker.run({'url': 'https://example.org/', 'mode': 'video', 'max_bytes': 1000, 'timeout_seconds': 30})
        media.assert_not_called()
        self.assertEqual(emitted[-1]['error'], 'site_rate_limited')
        self.assertEqual(emitted[-1]['retry_after_seconds'], 600)
        self.assertNotIn('pendiente', emitted[-1]['message'])

    def test_http_503_defers_to_the_queue_without_immediate_extractor_fallback(self):
        emitted = []
        error = HTTPError('https://example.org/', 503, 'Unavailable', {'Retry-After': '30'}, None)
        with tempfile.TemporaryDirectory() as temp, patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
             patch.object(worker.Path, 'cwd', return_value=Path(temp)), patch.object(worker, 'direct', side_effect=error), \
             patch.object(worker, 'media_download') as media, patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
            worker.run({'url': 'https://example.org/', 'mode': 'video', 'max_bytes': 1000, 'timeout_seconds': 30})
        media.assert_not_called()
        self.assertEqual(emitted[-1]['error'], 'download_temporary')
        self.assertEqual(emitted[-1]['retry_after_seconds'], 30)

    def test_extractor_rate_limit_and_unsupported_url_are_not_retried_immediately(self):
        for error, code in [(HTTPError('https://example.org/', 429, 'Too Many Requests', {'Retry-After': '600'}, None), 'site_rate_limited'),
                            (worker.DownloadError('ERROR: Unsupported URL: https://example.org/'), 'unsupported_download_url')]:
            emitted = []
            with tempfile.TemporaryDirectory() as temp, patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
                 patch.object(worker.Path, 'cwd', return_value=Path(temp)), patch.object(worker, 'direct', return_value={'links': []}), \
                 patch.object(worker, 'media_download', side_effect=error) as media, patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
                worker.run({'url': 'https://example.org/', 'mode': 'video', 'max_bytes': 1000, 'timeout_seconds': 30})
            media.assert_called_once()
            self.assertEqual(emitted[-1]['error'], code)
            if code == 'site_rate_limited':
                self.assertEqual(emitted[-1]['retry_after_seconds'], 600)


class DownloadPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / 'test.sqlite3')
        self.database.initialize()
        with self.database.connection() as connection:
            connection.execute("INSERT INTO users(id,username,password) VALUES(1,'fixture','unused')")
        self.conversation = self.database.create_conversation(1, 'general', 'local')['id']
        self.files = SimpleNamespace(agent=lambda: SimpleNamespace(downloads=DownloadConfig()))
        self.manager = self.new_manager()
        await self.manager.start()

    def new_manager(self):
        manager = DownloadJobs(self.database, self.files)
        manager.SITE_INTERVAL = .03
        manager.RATE_LIMIT_DELAY = .03
        manager.WAIT_POLL_SECONDS = .01
        manager.TRANSIENT_DELAY = .02
        return manager

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    def mock_runner(self, callback):
        runner = SimpleNamespace(run=AsyncMock(side_effect=callback))
        stub = patch('agent_core.download_jobs.Downloader', return_value=runner)
        stub.start()
        self.addCleanup(stub.stop)
        return runner

    async def submit(self, url='https://example.org/audio', mode='audio', conversation=None):
        result = await self.manager.submit(1, conversation or self.conversation, url, mode)
        return result['job']['id']

    async def wait_for(self, identifier, condition, conversation=None):
        async with asyncio.timeout(3):
            while True:
                job = self.manager.get(1, conversation or self.conversation, identifier)
                if condition(job):
                    return job
                await asyncio.sleep(.005)

    async def terminal(self, identifier, conversation=None):
        return await self.wait_for(identifier, lambda job: job['status'] in {'complete', 'failed', 'cancelled', 'blocked'}, conversation)

    def enable_persistence(self):
        self.files = SimpleNamespace(agent=lambda: SimpleNamespace(downloads=DownloadConfig(persistent_recovery=True)))
        self.manager.files = self.files

    async def test_persistent_rate_limit_continues_past_old_budget_until_verified(self):
        self.enable_persistence()
        calls = []
        async def run(*args, **kwargs):
            calls.append(1)
            if len(calls) <= 4:
                return {'ok': False, 'error': 'site_rate_limited'}
            return {'ok': True, 'download': {'filename': 'goal.mp3', 'sha256': 'verified'}}
        self.mock_runner(run)
        identifier = await self.submit()
        job = await self.terminal(identifier)
        self.assertEqual(len(calls), 5)
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['progress']['rate_limit_retries'], 4)
        self.assertEqual(job['objective']['status'], 'complete')
        self.assertTrue(job['progress']['retry_policy']['persistent_recovery'])

    async def test_persistent_temporary_errors_continue_and_cancellation_stops_them(self):
        self.enable_persistence()
        runner = self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'download_temporary'})
        identifier = await self.submit()
        pending = await self.wait_for(identifier, lambda job: job['progress'].get('transient_retries', 0) >= 3)
        self.assertEqual(pending['status'], 'queued')
        self.assertEqual(pending['objective']['status'], 'pending')
        await self.manager.cancel(1, self.conversation, identifier)
        count = runner.run.await_count
        await asyncio.sleep(.08)
        self.assertEqual(runner.run.await_count, count)
        self.assertEqual(self.manager.get(1, self.conversation, identifier)['objective']['status'], 'cancelled')

    async def test_access_requirement_keeps_goal_blocked_without_repeating_and_can_resume(self):
        self.enable_persistence()
        runner = self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'site_access_required', 'message': 'Sign in to confirm you are not a bot'})
        identifier = await self.submit()
        blocked = await self.terminal(identifier)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['objective']['blocker']['reason'], 'authorized_access_required')
        self.assertTrue(blocked['progress']['tool_inventory']['tools'])
        await self.manager.close()
        self.manager = self.new_manager()
        await self.manager.start()
        await asyncio.sleep(.05)
        self.assertEqual(runner.run.await_count, 1)
        runner.run.side_effect = lambda *args, **kwargs: {'ok': True, 'download': {'filename': 'authorized.mp3'}}
        repeated = await self.submit()
        self.assertEqual(repeated, identifier)
        self.assertEqual((await self.terminal(identifier))['status'], 'complete')

    async def test_persistent_retry_releases_the_site_for_other_objectives(self):
        self.enable_persistence()
        async def run(url, *args, **kwargs):
            if url.endswith('/first'):
                return {'ok': False, 'error': 'site_rate_limited'}
            return {'ok': True, 'download': {'filename': 'second.mp3'}}
        self.mock_runner(run)
        first = await self.submit('https://example.org/first')
        await self.wait_for(first, lambda job: job['progress'].get('phase') == 'waiting')
        second = await self.submit('https://example.org/second')
        self.assertEqual((await self.terminal(second))['status'], 'complete')
        self.assertIn(self.manager.get(1, self.conversation, first)['status'], {'queued', 'running'})
        await self.manager.cancel(1, self.conversation, first)

    async def test_blocked_notification_preserves_pending_objective_without_a_download(self):
        self.enable_persistence()
        self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'site_access_required', 'message': 'Acceso requerido'})
        identifier = await self.submit()
        self.manager.watch(1, self.conversation, identifier, 'watch-blocked')
        job = await self.terminal(identifier)
        notices = self.manager.notices(1, self.conversation)
        self.assertEqual(job['objective']['status'], 'blocked')
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['status'], 'blocked')
        self.assertIn('pendiente', notices[0]['content'])
        self.assertIsNone(notices[0]['download'])

    async def test_pruning_preserves_complete_sources_and_removes_old_partials(self):
        self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'fixture'})
        identifier = await self.submit()
        await self.terminal(identifier)
        stage = self.manager.stage(identifier)
        stage.mkdir(parents=True, exist_ok=True)
        complete = stage / 'source.mp3'
        complete.write_bytes(b'ID3complete')
        worker.remember_source({'path': complete, 'title': 'Complete'}, 'https://example.org/audio', 'audio', stage)
        (stage / 'old.part').write_bytes(b'partial')
        self.assertEqual(self.manager.prune_stage(identifier, 'https://example.org/audio', 'audio'), 1)
        self.assertTrue(complete.exists())
        self.assertTrue((stage / 'source.json').exists())
        self.assertFalse((stage / 'old.part').exists())

    async def test_delayed_retry_recovers_and_notifies_only_after_completion(self):
        calls = []
        async def run(*args, **kwargs):
            calls.append(time.monotonic())
            if len(calls) == 1:
                return {'ok': False, 'error': 'site_rate_limited', 'retry_after_seconds': .08}
            return {'ok': True, 'download': {'filename': 'fixture.mp3'}}
        self.mock_runner(run)
        identifier = await self.submit()
        waiting = await self.wait_for(identifier, lambda job: job['progress'].get('phase') == 'waiting')
        self.assertEqual(waiting['status'], 'queued')
        self.manager.watch(1, self.conversation, identifier, 'fixture-request')
        self.assertEqual(self.manager.notices(1, self.conversation), [])
        completed = await self.terminal(identifier)
        self.assertEqual(completed['status'], 'complete')
        self.assertGreaterEqual(calls[1] - calls[0], .075)
        self.assertEqual(len(self.manager.notices(1, self.conversation)), 1)
        self.assertEqual(self.manager.notices(1, self.conversation)[0]['status'], 'complete')

    async def test_temporary_connection_failure_recovers_with_persisted_wait_and_budget(self):
        calls = []
        async def run(*args, **kwargs):
            calls.append(time.monotonic())
            if len(calls) == 1:
                return {'ok': False, 'error': 'download_temporary', 'retry_after_seconds': .12}
            return {'ok': True, 'download': {'filename': 'recovered.mp3'}}
        self.mock_runner(run)
        identifier = await self.submit()
        waiting = await self.wait_for(identifier, lambda job: job['progress'].get('wait_reason') == 'temporary_error')
        self.assertEqual(waiting['progress']['transient_retries'], 1)
        self.assertTrue(any(step['kind'] == 'recovery_wait' for step in waiting['steps']))
        await self.manager.close()
        self.manager = self.new_manager()
        await self.manager.start()
        self.assertEqual((await self.terminal(identifier))['status'], 'complete')
        self.assertGreaterEqual(calls[1] - calls[0], .11)

    async def test_temporary_failures_exhaust_their_budget_without_claiming_success(self):
        runner = self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'download_temporary', 'message': 'Conexión fallida'})
        identifier = await self.submit()
        failed = await self.terminal(identifier)
        self.assertEqual(runner.run.await_count, 3)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['progress']['transient_retries'], 2)
        self.assertIn('sigue fallando', failed['message'])

    async def test_repeated_429_stops_after_two_automatic_retries_and_keeps_site_pause(self):
        runner = self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'site_rate_limited'})
        identifier = await self.submit()
        failed = await self.terminal(identifier)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(runner.run.await_count, 3)
        self.assertIn('agotaron', failed['message'])
        self.assertEqual(failed['progress']['rate_limit_retries'], 2)
        self.assertGreater(self.manager.site_state('example.org')['retry_at'], time.time())
        retry = await self.submit()
        self.assertEqual(retry, identifier)
        await asyncio.sleep(.025)
        self.assertEqual(runner.run.await_count, 3, 'Reintentar manualmente debe respetar la pausa del sitio')

    async def test_cooldown_is_shared_across_youtube_aliases_conversations_and_modes_without_blocking_other_sites(self):
        async def run(url, mode, **kwargs):
            if self.manager.site(url) == 'youtube.com':
                return {'ok': False, 'error': 'site_rate_limited', 'retry_after_seconds': 60}
            return {'ok': False, 'error': 'fixture', 'message': 'Otro sitio atendido'}
        runner = self.mock_runner(run)
        first = await self.submit('https://www.youtube.com/watch?v=abc')
        await self.wait_for(first, lambda job: job['progress'].get('phase') == 'waiting')
        other = self.database.create_conversation(1, 'general', 'local')['id']
        second = await self.submit('https://youtu.be/def', 'video', other)
        third = await self.submit('https://other.example/a')
        fourth = await self.submit('https://another.example/b')
        await self.terminal(third)
        await self.terminal(fourth)
        self.assertEqual(runner.run.await_count, 3)
        self.assertEqual(self.manager.get(1, other, second)['status'], 'queued')
        repeated = await self.submit('https://www.youtube.com/watch?v=abc')
        self.assertEqual(repeated, first)
        self.assertEqual(runner.run.await_count, 3)
        await self.manager.cancel(1, self.conversation, first)
        await self.manager.cancel(1, other, second)
        self.assertGreater(self.manager.site_state('youtube.com')['retry_at'], time.time())

    async def test_restart_preserves_pause_and_retry_budget_then_resumes(self):
        calls = []
        async def run(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return {'ok': False, 'error': 'site_rate_limited', 'retry_after_seconds': 60}
            return {'ok': True, 'download': {'filename': 'fixture.mp3'}}
        self.mock_runner(run)
        identifier = await self.submit()
        waiting = await self.wait_for(identifier, lambda job: job['progress'].get('phase') == 'waiting')
        expected_pause = waiting['progress']['retry_at']
        await self.manager.close()
        self.manager = self.new_manager()
        await self.manager.start()
        await asyncio.sleep(.04)
        self.assertEqual(len(calls), 1)
        recovered = self.manager.get(1, self.conversation, identifier)
        self.assertEqual(recovered['progress']['rate_limit_retries'], 1)
        self.assertEqual(recovered['progress']['retry_at'], expected_pause)
        with self.database.connection() as connection:
            connection.execute('UPDATE download_site_limits SET retry_at=?', (time.time() - 1,))
        self.assertEqual((await self.terminal(identifier))['status'], 'complete')
        self.assertEqual(len(calls), 2)

    async def test_same_site_downloads_are_serial_and_spaced(self):
        started, finished, active = [], [], []
        async def run(*args, **kwargs):
            started.append(time.monotonic())
            active.append(len(started) - len(finished))
            await asyncio.sleep(.02)
            finished.append(time.monotonic())
            return {'ok': False, 'error': 'fixture', 'message': 'Fixture'}
        self.mock_runner(run)
        first = await self.submit('https://www.example.org/a')
        second = await self.submit('https://example.org/b')
        await self.terminal(first)
        await self.terminal(second)
        self.assertEqual(max(active), 1)
        self.assertGreaterEqual(started[1] - finished[0], .025)

    async def test_access_failures_are_terminal_and_source_label_is_truthful(self):
        runner = self.mock_runner(lambda *args, **kwargs: {'ok': False, 'error': 'site_access_required', 'message': 'Acceso requerido'})
        identifier = await self.submit()
        failed = await self.terminal(identifier)
        self.assertEqual(runner.run.await_count, 1)
        self.assertEqual(failed['status'], 'blocked')
        self.assertEqual(failed['objective']['status'], 'blocked')
        self.assertFalse(failed['source_available'])
        stage = self.manager.stage(identifier)
        stage.mkdir(parents=True)
        path = stage / 'original.mp3'
        path.write_bytes(b'ID3fixture audio')
        worker.remember_source({'path': path, 'title': 'Fixture'}, 'https://example.org/audio', 'audio', stage)
        self.assertTrue(self.manager.get(1, self.conversation, identifier)['source_available'])
        path.unlink()
        self.assertFalse(self.manager.get(1, self.conversation, identifier)['source_available'])


if __name__ == '__main__':
    unittest.main()
