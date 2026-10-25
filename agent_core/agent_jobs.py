"""Cola privada persistente; AgentService conserva la autoría de los mensajes.

Un único proceso del servidor posee esta cola. Los trabajos se recuperan al
reiniciar, sin reservar ejecuciones mientras esperan recursos ni repetir un
turno que AgentService ya haya confirmado en SQLite.
"""
import asyncio
import json
import time
import uuid

from .config import AgentError
from .security import token_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_jobs(
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    request_id TEXT NOT NULL,
    input_text TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed','cancelled')),
    message TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(conversation_id,request_id)
);
CREATE INDEX IF NOT EXISTS agent_jobs_pending ON agent_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS agent_jobs_user ON agent_jobs(user_id,status);
"""

BUSY_CODES = {"agent_busy", "conversation_busy", "rate_limit"}
ERROR_MESSAGES = {
    "provider_timeout": "El proveedor tardó demasiado. Puedes volver a intentarlo.",
    "run_timeout": "El trabajo agotó su tiempo máximo. Puedes reducir el alcance y reintentar.",
    "execution_limit": "Se alcanzó el límite de pasos. La tarea quedó pendiente.",
    "context_limit": "La petición supera el contexto disponible del modelo.",
    "profile_context_limit": "El modelo no dispone del contexto que necesita este perfil.",
    "prompt_too_long": "Este intento superó el antiguo límite de tamaño de instrucciones. Vuelve a enviar tu mensaje; ese límite fijo ya se retiró.",
    "invalid_prompt": "No se pudieron cargar las instrucciones del perfil. Consulta la consola y la documentación del proyecto.",
    "source_not_consulted": "El agente propuso usar un enlace que no estaba entre las fuentes válidas de la conversación.",
    "provider_auth": "El proveedor rechazó la autenticación. Revisa su configuración.",
    "missing_api_key": "Falta configurar la clave del proveedor en el servidor.",
    "provider_unavailable": "No se pudo conectar con el proveedor del modelo.",
    "laboratory_tools_required": "Este perfil necesita un modelo que permita usar herramientas.",
    "sandbox_unavailable": "El entorno de ejecución no está disponible.",
    "request_conflict": "Ese identificador ya pertenece a otro mensaje.",
}


class AgentJobs:
    def __init__(self, database, files, service):
        self.database, self.files, self.service = database, files, service
        self.tasks = {}
        self._task_users = {}
        self._user_locks = {}
        self.closing = False
        self.started = False
        self.retry_delay = .5
        config = self.files.agent()
        team = getattr(config, "multi_agent", None)
        parallel = getattr(team, "max_parallel_jobs", 2)
        self.semaphore = asyncio.Semaphore(max(1, min(parallel, config.limits.max_concurrent_runs)))
        self.max_pending = max(1, min(getattr(team, "max_pending_jobs", 20), 100))
        self._schema_ready = False

    def ensure_schema(self):
        if not self._schema_ready:
            with self.database.connection() as connection:
                connection.executescript(SCHEMA)
            self._schema_ready = True

    @staticmethod
    def _record(row):
        # El prompt se devuelve únicamente después de comprobar al propietario;
        # sólo AgentService añade mensajes al historial de la conversación.
        value = {key: row[key] for key in (
            "id", "conversation_id", "request_id", "status", "message", "error_code", "created_at", "updated_at")}
        value["prompt"] = row["input_text"]
        value["result"] = json.loads(row["result_json"])
        return value

    def list(self, user_id, conversation_id):
        self.database.conversation(user_id, conversation_id)
        self.ensure_schema()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_jobs WHERE user_id=? AND conversation_id=? ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END,rowid DESC LIMIT 30",
                (user_id, conversation_id)).fetchall()
        return [self._record(row) for row in rows]

    def get(self, user_id, conversation_id, identifier):
        self.database.conversation(user_id, conversation_id)
        self.ensure_schema()
        with self.database.connection() as connection:
            # request_id es el identificador que ya conoce el navegador.
            row = connection.execute(
                "SELECT * FROM agent_jobs WHERE user_id=? AND conversation_id=? AND (id=? OR request_id=?) ORDER BY id=? DESC LIMIT 1",
                (user_id, conversation_id, identifier, identifier, identifier)).fetchone()
        if row is None:
            raise AgentError("agent_job_missing", "El trabajo no existe en esta conversación.", 404)
        return self._record(row)

    def _update(self, identifier, **fields):
        allowed = {"status", "message", "error_code", "result_json"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Campos de trabajo no válidos")
        with self.database.connection() as connection:
            connection.execute("UPDATE agent_jobs SET " + ",".join(key + "=?" for key in fields)
                               + ",updated_at=CURRENT_TIMESTAMP WHERE id=?", [*fields.values(), identifier])

    def _event(self, row, kind, title):
        self.database.event(row["conversation_id"], row["request_id"], kind, title,
                            {"job_id": row["id"]})

    def _fail_matching_run(self, row, code):
        with self.database.connection() as connection:
            connection.execute("""UPDATE agent_runs SET status='failed',error_code=?
                WHERE status='running' AND user_id=? AND conversation_id=?
                AND request_id=? AND message_hash=?""", (code, row["user_id"],
                row["conversation_id"], row["request_id"], row["message_hash"]))

    def schedule(self, identifier, user_id):
        if identifier not in self.tasks and not self.closing:
            self._task_users[identifier] = user_id
            self.tasks[identifier] = asyncio.create_task(self._run(identifier, user_id))

    async def start(self):
        if self.started:
            return
        self.ensure_schema()
        self.closing = False
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Sólo se desbloquean ejecuciones de esta cola, con propietario y
            # hash exactos; las consultas síncronas ajenas no se modifican.
            connection.execute("""UPDATE agent_runs SET status='failed',error_code='job_interrupted'
                WHERE status='running' AND EXISTS(
                    SELECT 1 FROM agent_jobs j WHERE j.status IN ('queued','running')
                    AND j.conversation_id=agent_runs.conversation_id
                    AND j.user_id=agent_runs.user_id AND j.request_id=agent_runs.request_id
                    AND j.message_hash=agent_runs.message_hash)""")
            rows = connection.execute(
                "SELECT * FROM agent_jobs WHERE status IN ('queued','running') ORDER BY rowid").fetchall()
            connection.execute("""UPDATE agent_jobs SET status='queued',error_code=NULL,
                message='Trabajo recuperado; continuará tras reiniciar el servidor.',updated_at=CURRENT_TIMESTAMP
                WHERE status IN ('queued','running')""")
        self.started = True
        for row in rows:
            self._event(row, "agent_job_recovered", "Trabajo recuperado tras reiniciar el servidor")
            self.schedule(row["id"], row["user_id"])

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self._task_users.clear()
        self._user_locks.clear()
        self.started = False

    async def submit(self, user_id, conversation_id, request_id, message):
        self.database.conversation(user_id, conversation_id)
        config = self.files.agent()
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 128
                or not isinstance(message, str) or not message.strip()):
            raise AgentError("invalid_agent_job", "Escribe un mensaje e identificador válidos.", 422)
        if len(message) > config.limits.max_message_chars:
            raise AgentError("message_limit", "El mensaje supera el límite de caracteres configurado.", 422)
        if self.closing:
            raise AgentError("agent_restarting", "El servidor se está reiniciando. Reintenta en unos segundos.", 503)
        self.ensure_schema()
        cached = self.database.cached_run(user_id, conversation_id, request_id, message)
        fingerprint = token_hash(message)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM agent_jobs WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id)).fetchone()
            if previous:
                if previous["message_hash"] != fingerprint or previous["user_id"] != user_id:
                    raise AgentError("request_conflict", "Ese identificador ya pertenece a otro mensaje.", 409)
                identifier, status = previous["id"], previous["status"]
            else:
                if cached is None:
                    global_count, user_count = connection.execute(
                        "SELECT COUNT(*),COALESCE(SUM(user_id=?),0) FROM agent_jobs WHERE status IN ('queued','running')",
                        (user_id,)).fetchone()
                    if global_count >= self.max_pending or user_count >= min(5, self.max_pending):
                        raise AgentError("agent_job_limit", "Hay demasiados trabajos pendientes. Espera a que termine uno o cancélalo.", 429)
                identifier, status = str(uuid.uuid4()), "complete" if cached is not None else "queued"
                connection.execute("""INSERT INTO agent_jobs
                    (id,conversation_id,user_id,request_id,input_text,message_hash,status,message,result_json)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (identifier, conversation_id, user_id, request_id, message,
                    fingerprint, status, "Respuesta disponible." if cached is not None else "Trabajo en cola.",
                    json.dumps(cached if cached is not None else {}, ensure_ascii=False, allow_nan=False)))
        if status in {"queued", "running"}:
            if previous is None:
                self._event({"id": identifier, "conversation_id": conversation_id, "request_id": request_id},
                            "agent_job_queued", "Trabajo recibido en segundo plano")
            self.schedule(identifier, user_id)
        return {"ok": True, "status": status, "job": self.get(user_id, conversation_id, identifier)}

    async def cancel(self, user_id, conversation_id, identifier):
        job = self.get(user_id, conversation_id, identifier)
        if job["status"] == "complete":
            raise AgentError("agent_job_complete", "La respuesta ya está disponible.", 409)
        if job["status"] == "cancelled":
            return job
        task = self.tasks.get(job["id"])
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(job["id"], None)
            self._task_users.pop(job["id"], None)
            if user_id not in self._task_users.values():
                self._user_locks.pop(user_id, None)
        # Un servicio que finaliza atómicamente justo al cancelar prevalece.
        current = self.get(user_id, conversation_id, job["id"])
        if current["status"] == "complete":
            raise AgentError("agent_job_complete", "La respuesta ya está disponible.", 409)
        self._update(job["id"], status="cancelled", message="Trabajo cancelado.", error_code="cancelled", result_json="{}")
        if current["status"] != "cancelled":
            self._event(job, "agent_job_cancelled", "Trabajo cancelado por su propietario")
        return self.get(user_id, conversation_id, job["id"])

    def _busy(self, user_id, conversation_id):
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT user_id,conversation_id FROM agent_runs WHERE status='running' AND expires_at>?",
                (time.time(),)).fetchall()
        maximum = self.files.agent().limits.max_concurrent_runs
        return len(rows) >= maximum or any(row["user_id"] == user_id or row["conversation_id"] == conversation_id for row in rows)

    async def _run(self, identifier, user_id):
        row = None
        executing = False
        try:
            lock = self._user_locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                waiting = False
                announced = False
                while True:
                    with self.database.connection() as connection:
                        row = connection.execute("SELECT * FROM agent_jobs WHERE id=? AND user_id=?", (identifier, user_id)).fetchone()
                    if row is None or row["status"] not in {"queued", "running"}:
                        return
                    if self._busy(user_id, row["conversation_id"]):
                        if not waiting:
                            self._update(identifier, status="queued", message="Esperando a que se liberen los recursos del agente.")
                            self._event(row, "agent_job_waiting", "Trabajo en cola: esperando recursos disponibles")
                            waiting = True
                        await asyncio.sleep(self.retry_delay)
                        continue
                    retry = False
                    async with self.semaphore:
                        self._update(identifier, status="running", message="El agente está trabajando en segundo plano.", error_code=None)
                        if not announced:
                            self._event(row, "agent_job_started", "Trabajo en segundo plano iniciado")
                            announced = True
                        try:
                            config = self.files.agent()
                            timeout = getattr(getattr(config, "multi_agent", None), "timeout_seconds", config.limits.run_timeout_seconds)
                            async with asyncio.timeout(timeout):
                                executing = True
                                result = await self.service.respond(user_id, row["conversation_id"], row["request_id"], row["input_text"])
                        except AgentError as error:
                            if error.code not in BUSY_CODES:
                                raise
                            executing = False
                            self._update(identifier, status="queued", message="Esperando a que se liberen los recursos del agente.")
                            if not waiting:
                                self._event(row, "agent_job_waiting", "Trabajo en cola: esperando recursos disponibles")
                            waiting, retry = True, True
                        else:
                            if not isinstance(result, dict) or not isinstance(result.get("answer"), str) or not result["answer"].strip():
                                raise AgentError("invalid_job_result", "No se obtuvo una respuesta válida.", 502)
                            serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
                            self._update(identifier, status="complete", message="Respuesta disponible; consulta sus resultados y comprobaciones.",
                                         result_json=serialized, error_code=None)
                            self._event(row, "agent_job_complete", "Respuesta del trabajo disponible")
                            return
                    if retry:
                        await asyncio.sleep(self.retry_delay)
        except asyncio.CancelledError:
            self._update(identifier, status="queued" if self.closing else "cancelled", error_code=None if self.closing else "cancelled",
                         message="El trabajo continuará al reiniciar el servidor." if self.closing else "Trabajo cancelado.", result_json="{}")
            if row is not None:
                if executing:
                    self._fail_matching_run(row, "job_interrupted" if self.closing else "cancelled")
                self._event(row, "agent_job_paused" if self.closing else "agent_job_cancelled",
                            "Trabajo conservado para reiniciar" if self.closing else "Trabajo cancelado por su propietario")
            raise
        except Exception as error:
            # No se guardan repr/tracebacks, cabeceras ni textos arbitrarios del proveedor.
            code = ("run_timeout" if isinstance(error, TimeoutError) else
                    error.code if isinstance(error, AgentError) and error.code in ERROR_MESSAGES else "agent_job_failed")
            self._update(identifier, status="failed", error_code=code,
                         message=ERROR_MESSAGES.get(code, "El trabajo no pudo completarse. Consulta el último error en la consola."), result_json="{}")
            if row is not None:
                if executing:
                    self._fail_matching_run(row, code)
                self._event(row, "agent_job_failed", "El trabajo no pudo completarse")
        finally:
            self.tasks.pop(identifier, None)
            self._task_users.pop(identifier, None)
            if user_id not in self._task_users.values():
                self._user_locks.pop(user_id, None)
