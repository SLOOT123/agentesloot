"""Cola SQLite real y ejecutor simulado; nunca llama a un proveedor externo."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from agent_core.agent_jobs import AgentJobs
from agent_core.config import AgentError
from agent_core.security import token_hash
from agent_core.storage import Database


class StoredService:
    def __init__(self, database):
        self.database = database
        self.calls = []
        self.release = None
        self.active = set()
        self.peak = 0
        self.cancelled = []
        self.failures = []

    async def respond(self, user_id, conversation_id, request_id, message):
        cached = self.database.cached_run(user_id, conversation_id, request_id, message)
        if cached is not None:
            return cached
        if self.failures:
            raise self.failures.pop(0)
        self.database.start_run(user_id, conversation_id, request_id, message, 30, 2)
        self.calls.append((user_id, conversation_id, request_id, message))
        if user_id in self.active:
            raise AssertionError("No se pueden solapar dos trabajos del mismo usuario")
        self.active.add(user_id)
        self.peak = max(self.peak, len(self.active))
        try:
            if self.release is not None:
                await self.release.wait()
            await asyncio.sleep(0)
            return self.database.finish_run(user_id, conversation_id, request_id, message,
                                            "Respuesta comprobada de prueba", [], {"review": "fixture"})
        except asyncio.CancelledError:
            self.cancelled.append(request_id)
            self.database.fail_run(conversation_id, request_id, "cancelled")
            raise
        finally:
            self.active.discard(user_id)


class AgentJobsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "test.sqlite3")
        self.database.initialize()
        with self.database.connection() as connection:
            self.user = connection.execute("INSERT INTO users(username,password) VALUES('uno','unused')").lastrowid
            self.other = connection.execute("INSERT INTO users(username,password) VALUES('dos','unused')").lastrowid
        self.conversation = self.database.create_conversation(self.user, "agent", "local")["id"]
        self.second = self.database.create_conversation(self.user, "agent", "local")["id"]
        self.foreign = self.database.create_conversation(self.other, "agent", "local")["id"]
        self.config = SimpleNamespace(limits=SimpleNamespace(max_concurrent_runs=2, max_message_chars=8000, run_timeout_seconds=30),
                                      multi_agent=SimpleNamespace(max_parallel_jobs=2, max_pending_jobs=20, timeout_seconds=30))
        self.files = SimpleNamespace(agent=lambda: self.config)
        self.service = StoredService(self.database)
        self.jobs = AgentJobs(self.database, self.files, self.service)
        self.jobs.retry_delay = .01
        await self.jobs.start()

    async def asyncTearDown(self):
        await self.jobs.close()
        self.temp.cleanup()

    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.005)

    async def complete(self, request_id, conversation=None, user=None):
        conversation, user = conversation or self.conversation, user or self.user
        await self.wait_until(lambda: self.jobs.get(user, conversation, request_id)["status"] in {"complete", "failed", "cancelled"})
        return self.jobs.get(user, conversation, request_id)

    async def test_submit_is_idempotent_owned_and_conflicts_do_not_change_input(self):
        first = await self.jobs.submit(self.user, self.conversation, "request-one", "Busca documentación")
        duplicate = await self.jobs.submit(self.user, self.conversation, "request-one", "Busca documentación")
        self.assertEqual(first["job"]["id"], duplicate["job"]["id"])
        self.assertEqual(self.jobs.get(self.user, self.conversation, "request-one")["prompt"], "Busca documentación")
        with self.assertRaises(AgentError) as conflict:
            await self.jobs.submit(self.user, self.conversation, "request-one", "Otro mensaje")
        self.assertEqual(conflict.exception.code, "request_conflict")
        for operation in (lambda: self.jobs.get(self.other, self.conversation, "request-one"),
                          lambda: self.jobs.get(self.other, self.foreign, first["job"]["id"]),
                          lambda: self.jobs.list(self.other, self.conversation)):
            with self.assertRaises(AgentError) as denied:
                operation()
            self.assertEqual(denied.exception.status, 404)
        with self.assertRaises(AgentError):
            await self.jobs.cancel(self.other, self.conversation, "request-one")
        result = await self.complete("request-one")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(len(self.database.history(self.user, self.conversation)), 2)
        replay = await self.jobs.submit(self.user, self.conversation, "request-one", "Busca documentación")
        self.assertEqual(replay["job"]["result"], result["result"])
        self.assertNotIn("message_hash", result)
        with self.assertRaises(AgentError) as finished:
            await self.jobs.cancel(self.user, self.conversation, "request-one")
        self.assertEqual(finished.exception.status, 409)

    async def test_multiple_users_run_in_parallel_and_same_user_waits_without_failure(self):
        self.service.release = asyncio.Event()
        await self.jobs.submit(self.user, self.conversation, "one", "Primero")
        await self.jobs.submit(self.user, self.second, "two", "Segundo")
        await self.jobs.submit(self.other, self.foreign, "three", "Tercero")
        await self.wait_until(lambda: len(self.service.calls) == 2)
        self.assertEqual(self.service.peak, 2)
        self.assertEqual(self.jobs.get(self.user, self.second, "two")["status"], "queued")
        self.assertEqual({item[0] for item in self.service.calls}, {self.user, self.other})
        self.service.release.set()
        for request, conversation, user in (("one", self.conversation, self.user), ("two", self.second, self.user), ("three", self.foreign, self.other)):
            self.assertEqual((await self.complete(request, conversation, user))["status"], "complete")
        self.assertEqual(len(self.service.calls), 3)

    async def test_existing_synchronous_run_and_transient_busy_stay_queued(self):
        self.database.start_run(self.user, self.second, "sync", "Mensaje ajeno", 30, 2)
        await self.jobs.submit(self.user, self.conversation, "waiting", "Esperar")
        await asyncio.sleep(.04)
        self.assertEqual(self.jobs.get(self.user, self.conversation, "waiting")["status"], "queued")
        self.assertEqual(self.service.calls, [])
        self.database.fail_run(self.second, "sync", "fixture_complete")
        self.service.failures = [AgentError("agent_busy", "No debe fallar", 429), AgentError("rate_limit", "No debe fallar", 429)]
        self.assertEqual((await self.complete("waiting"))["status"], "complete")
        self.assertEqual(len(self.service.calls), 1)

    async def test_cancel_propagates_to_executor_and_does_not_publish_a_turn(self):
        self.service.release = asyncio.Event()
        await self.jobs.submit(self.user, self.conversation, "running", "Trabajo largo")
        await self.wait_until(lambda: len(self.service.calls) == 1)
        result = await self.jobs.cancel(self.user, self.conversation, "running")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.service.cancelled, ["running"])
        self.assertEqual(self.database.history(self.user, self.conversation), [])
        self.assertEqual(self.database.activity(self.user, self.conversation, "running")["status"], "failed")
        self.assertTrue(any(event["kind"] == "agent_job_cancelled" for event in self.database.activity(self.user, self.conversation, "running")["events"]))
        immediate = await self.jobs.submit(self.user, self.second, "before-start", "Cancelar en cola")
        await self.jobs.cancel(self.user, self.second, immediate["job"]["id"])
        self.assertNotIn(immediate["job"]["id"], self.jobs.tasks)
        self.assertEqual(len(self.service.calls), 1)

    async def test_shutdown_resumes_only_matching_owned_runs(self):
        self.service.release = asyncio.Event()
        queued = await self.jobs.submit(self.user, self.conversation, "resume", "Persistente")
        await self.wait_until(lambda: len(self.service.calls) == 1)
        await self.jobs.close()
        self.assertEqual(self.jobs.get(self.user, self.conversation, "resume")["status"], "queued")
        # Simula caída brusca después de reservar una ejecución, sin close().
        with self.database.connection() as connection:
            connection.execute("UPDATE agent_jobs SET status='running' WHERE id=?", (queued["job"]["id"],))
            connection.execute("UPDATE agent_runs SET status='running',expires_at=? WHERE request_id='resume'", (time.time() + 100,))
        self.database.start_run(self.other, self.foreign, "unrelated", "Consulta ajena", 30, 2)
        restored_service = StoredService(self.database)
        self.jobs = AgentJobs(self.database, self.files, restored_service)
        await self.jobs.start()
        with self.database.connection() as connection:
            own = connection.execute("SELECT status,error_code FROM agent_runs WHERE request_id='resume'").fetchone()
            unrelated = connection.execute("SELECT status FROM agent_runs WHERE request_id='unrelated'").fetchone()
        self.assertEqual(tuple(own), ("failed", "job_interrupted"))
        self.assertEqual(unrelated[0], "running")
        result = await self.complete("resume")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(restored_service.calls), 1)
        self.assertEqual(len(self.database.history(self.user, self.conversation)), 2)
        self.assertTrue(any(event["kind"] == "agent_job_recovered" for event in self.database.activity(self.user, self.conversation, "resume")["events"]))

    async def test_cancelling_a_waiting_job_never_changes_an_unrelated_running_request(self):
        await self.jobs.submit(self.user, self.conversation, "shared-id", "Trabajo en cola")
        self.database.start_run(self.user, self.conversation, "shared-id", "Petición externa con otro hash", 30, 2)
        await asyncio.sleep(.03)
        self.assertEqual(self.jobs.get(self.user, self.conversation, "shared-id")["status"], "queued")
        await self.jobs.cancel(self.user, self.conversation, "shared-id")
        with self.database.connection() as connection:
            run = connection.execute("SELECT status,message_hash FROM agent_runs WHERE request_id='shared-id'").fetchone()
        self.assertEqual(tuple(run), ("running", token_hash("Petición externa con otro hash")))
        self.assertEqual(self.service.calls, [])

    async def test_committed_service_response_survives_crash_without_duplicate_messages(self):
        item = await self.jobs.submit(self.user, self.conversation, "committed", "Ya resuelto")
        await self.jobs.close()
        self.database.start_run(self.user, self.conversation, "committed", "Ya resuelto", 30, 2)
        committed = self.database.finish_run(self.user, self.conversation, "committed", "Ya resuelto", "Resultado real", [], {})
        with self.database.connection() as connection:
            connection.execute("UPDATE agent_jobs SET status='running' WHERE id=?", (item["job"]["id"],))
        self.jobs = AgentJobs(self.database, self.files, self.service)
        await self.jobs.start()
        result = await self.complete("committed")
        self.assertEqual(result["result"], committed)
        self.assertEqual(len(self.database.history(self.user, self.conversation)), 2)
        self.assertEqual(self.service.calls, [])

    async def test_recovery_never_modifies_a_run_with_an_unmatched_hash(self):
        item = await self.jobs.submit(self.user, self.conversation, "conflict", "Solicitud original")
        await self.jobs.close()
        self.database.start_run(self.user, self.conversation, "conflict", "Contenido diferente", 30, 2)
        self.jobs = AgentJobs(self.database, self.files, self.service)
        await self.jobs.start()
        with self.database.connection() as connection:
            run = connection.execute("SELECT status,message_hash FROM agent_runs WHERE request_id='conflict'").fetchone()
        self.assertEqual(tuple(run), ("running", token_hash("Contenido diferente")))
        # Deja de estar ocupado y la comprobación de idempotencia detecta el conflicto.
        self.database.fail_run(self.conversation, "conflict", "fixture_done")
        result = await self.complete("conflict")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "request_conflict")

    async def test_unconsulted_source_reports_the_cause_without_exposing_the_proposed_url(self):
        self.service.failures = [AgentError('source_not_consulted', 'URL interna no exponer', 422)]
        await self.jobs.submit(self.user, self.conversation, 'invalid-source', 'Dame un ejemplo')
        result = await self.complete('invalid-source')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'source_not_consulted')
        self.assertIn('fuentes válidas', result['message'])
        self.assertNotIn('URL interna', result['message'])
        self.assertEqual(result['result'], {})
        self.assertEqual(self.database.history(self.user, self.conversation), [])

    async def test_failures_and_timeouts_do_not_leak_provider_details_or_claim_completion(self):
        self.service.failures = [RuntimeError("fixture-secret-api-key no exponer")]
        await self.jobs.submit(self.user, self.conversation, "broken", "Consulta")
        result = await self.complete("broken")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"], {})
        with self.database.connection() as connection:
            encoded = "\n".join(str(tuple(row)) for row in connection.execute("SELECT * FROM agent_jobs"))
            events = "\n".join(str(tuple(row)) for row in connection.execute("SELECT * FROM agent_events"))
        self.assertNotIn("fixture-secret-api-key", encoded + events)
        self.assertEqual(self.database.history(self.user, self.conversation), [])
        self.service.release = asyncio.Event()
        self.config.multi_agent.timeout_seconds = .02
        await self.jobs.submit(self.user, self.conversation, "timeout", "Largo")
        timed_out = await self.complete("timeout")
        self.assertEqual(timed_out["status"], "failed")
        self.assertEqual(timed_out["error_code"], "run_timeout")
        self.assertIn("timeout", self.service.cancelled)

    async def test_pending_limits_apply_atomically_and_completed_jobs_do_not_consume_capacity(self):
        self.jobs.max_pending = 2
        self.service.release = asyncio.Event()
        await self.jobs.submit(self.user, self.conversation, "limit-one", "Uno")
        await self.jobs.submit(self.other, self.foreign, "limit-two", "Dos")
        with self.assertRaises(AgentError) as limit:
            await self.jobs.submit(self.user, self.second, "limit-three", "Tres")
        self.assertEqual(limit.exception.code, "agent_job_limit")
        await self.jobs.cancel(self.other, self.foreign, "limit-two")
        third = await self.jobs.submit(self.user, self.second, "limit-three", "Tres")
        self.assertEqual(third["status"], "queued")


if __name__ == "__main__":
    unittest.main()
