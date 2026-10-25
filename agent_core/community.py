"""Respuestas públicas en cola; su contexto proviene únicamente del canal compartido."""
import asyncio
import json
import uuid
from contextlib import nullcontext

from .config import AgentError
from .providers import Provider
from .service import context_cost
from .team_control import model_gate


class CommunityAgent:
    def __init__(self, database, files, transport=None):
        self.database, self.files, self.transport = database, files, transport
        self.task = None

    async def start(self):
        with self.database.connection() as connection:
            connection.execute("UPDATE community_replies SET status='queued' WHERE status='running'")
        self.wake()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def wake(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def submit(self, user, message, request_id=None):
        identifier = str(request_id or uuid.uuid4())
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("""SELECT j.message_id,j.status,m.message FROM community_replies j
                JOIN messages m ON m.id=j.message_id WHERE j.user_id=? AND j.request_id=?""", (user["id"], identifier)).fetchone()
            if previous:
                if previous["message"] != message:
                    raise AgentError("request_conflict", "Ese identificador ya pertenece a otro mensaje.", 409)
                result = {"status": "saved", "message_id": previous["message_id"], "assistant_status": previous["status"]}
            else:
                pending = connection.execute("SELECT user_id FROM community_replies WHERE status IN ('queued','running')").fetchall()
                if len(pending) >= 20 or sum(row["user_id"] == user["id"] for row in pending) >= 5:
                    raise AgentError("community_busy", "Hay varias respuestas en cola. Espera a que termine una antes de enviar otro mensaje.", 429)
                message_id = connection.execute("INSERT INTO messages(username,message,role) VALUES(?,?,'user')", (user["username"], message)).lastrowid
                connection.execute("INSERT INTO community_replies(message_id,user_id,request_id,status) VALUES(?,?,?,'queued')", (message_id, user["id"], identifier))
                result = {"status": "saved", "message_id": message_id, "assistant_status": "queued"}
        self.wake()
        return result

    def context(self, message_id, backend):
        with self.database.connection() as connection:
            rows = connection.execute("""SELECT username,message,role FROM messages
                WHERE (role='user' AND id<=?) OR (role='assistant' AND reply_to<?)
                ORDER BY COALESCE(reply_to,id) DESC,id DESC LIMIT 30""", (message_id, message_id)).fetchall()
        history = [{"role": row["role"], "content": (row["username"] + ": " if row["role"] == "user" else "") + row["message"]} for row in reversed(rows)]
        prompt = (self.files.root / "prompts/community.md").read_text("utf-8")
        system = {"role": "system", "content": prompt}
        output = min(512, backend.max_output_tokens)
        budget = backend.context_window - output - 256
        selected = [history[-1]]
        if context_cost([system, *selected], []) > budget:
            raise AgentError("context_limit", "El mensaje supera el contexto disponible del modelo.", 422)
        for item in reversed(history[:-1]):
            if context_cost([system, item, *selected], []) > budget:
                break
            selected.insert(0, item)
        # Evita comenzar con una respuesta huérfana si hubo que recortar el historial.
        while len(selected) > 1 and selected[0]["role"] == "assistant":
            selected.pop(0)
        return [system, *selected], output

    async def run(self):
        while True:
            with self.database.connection() as connection:
                job = connection.execute("SELECT j.message_id,j.user_id,m.message FROM community_replies j JOIN messages m ON m.id=j.message_id WHERE j.status='queued' ORDER BY j.message_id LIMIT 1").fetchone()
                if job is None:
                    return
                identifier = job["message_id"]
                connection.execute("UPDATE community_replies SET status='running',updated_at=CURRENT_TIMESTAMP WHERE message_id=?", (identifier,))
            try:
                config = self.files.agent()
                backend = config.backends[config.default_backend]
                messages, output = self.context(identifier, backend)
                from .team_research import TeamResearch, research_requested
                sources, team_status = [], None
                if config.multi_agent.enabled and research_requested(job["message"], messages[1:-1]):
                    from .team_control import SkillRegistry, PublicTeamCheckpoints
                    from .tools import Tools
                    skill = SkillRegistry(self.files.root).load("research")
                    checkpoints = PublicTeamCheckpoints(self.database, identifier, job["message"], skill)
                    def worker_tools(worker_id):
                        worker = Tools(self.database, job["user_id"], config.limits.request_timeout_seconds, self.transport, sandbox=config.sandbox)
                        worker.user_links(job["message"])
                        return worker
                    team = TeamResearch(config.multi_agent, Provider(backend, config.limits, self.transport), worker_tools,
                                        event=lambda kind, title, item=None: checkpoints("event:" + kind, {"title": title, "payload": item or {}}),
                                        checkpointer=checkpoints, skill=skill)
                    result = await team.run(job["message"], messages[1:-1])
                    answer, sources, team_status = result["answer"], result["sources"], result["team"]["status"]
                else:
                    async with asyncio.timeout(config.limits.run_timeout_seconds):
                        async with (model_gate(config.multi_agent) if config.multi_agent.enabled else nullcontext()):
                            turn = await Provider(backend, config.limits, self.transport).complete(messages, output_limit=output)
                    if turn.calls or not turn.content.strip():
                        raise AgentError("invalid_model_response", "La IA no devolvió una respuesta de texto válida.", 502)
                    answer = turn.content.strip()
                with self.database.connection() as connection:
                    # Respuesta y estado se guardan en una única transacción; no se encola una respuesta del bot.
                    reply = connection.execute("INSERT INTO messages(username,message,role,reply_to,sources_json,team_status) VALUES('Agente',?,'assistant',?,?,?)", (answer, identifier, json.dumps(sources, ensure_ascii=False), team_status)).lastrowid
                    connection.execute("UPDATE community_replies SET status='complete',reply_id=?,error_code=NULL,error_message='',updated_at=CURRENT_TIMESTAMP WHERE message_id=?", (reply, identifier))
            except asyncio.CancelledError:
                with self.database.connection() as connection:
                    connection.execute("UPDATE community_replies SET status='queued' WHERE message_id=?", (identifier,))
                raise
            except Exception as error:
                code = getattr(error, "code", "community_agent_failed")
                detail = getattr(error, "message", "No se pudo obtener la respuesta de la IA. Inténtalo de nuevo.")
                if isinstance(error, TimeoutError):
                    code, detail = "community_timeout", "La IA agotó el tiempo de respuesta. Inténtalo de nuevo."
                with self.database.connection() as connection:
                    connection.execute("UPDATE community_replies SET status='failed',error_code=?,error_message=?,updated_at=CURRENT_TIMESTAMP WHERE message_id=?", (code, detail, identifier))
