"""Skills versionados, evidencias privadas y especialistas sin permisos de escritura."""
import asyncio
import hashlib
import json
import re
import weakref

from pydantic import BaseModel, ConfigDict, Field

from .config import AgentError
from .security import token_hash


class SkillSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    purpose: str = Field(min_length=1, max_length=300)
    tools: list[str] = Field(min_length=1, max_length=15)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)
    repair_policy: str = Field(min_length=1, max_length=500)


class SkillRegistry:
    def __init__(self, root):
        self.root = root

    def load(self, name):
        if name not in {"research", "coding", "math", "download"}:
            raise AgentError("unknown_skill", "El propósito no tiene un skill registrado.", 422)
        try:
            skill = SkillSpec.model_validate_json((self.root / "agent_skills" / (name + ".json")).read_text("utf-8"))
            if skill.id != name or any(len(item) > 300 for item in skill.acceptance_criteria):
                raise ValueError()
            return skill.model_dump()
        except (ValueError, OSError):
            raise AgentError("invalid_skill", "Revisa el contrato del skill " + name + ".", 422) from None


def execution_skill(message):
    text = message.casefold()
    if re.search(r"\b(calcula\w*|resuelve\w*|ecuaci[oó]n\w*|matem[aá]tica\w*|derivada|integral)\b", text):
        return "math"
    if re.search(r"\b(c[oó]digo|programa\w*|script|proyectos?|archivos?|pruebas?|tests?|funci[oó]n|python|javascript|html)\b", text) and re.search(r"\b(crea\w*|escrib\w*|genera\w*|ejecut\w*|corrig\w*|arregl\w*|comprueba\w*|implement\w*|desarroll\w*)\b", text):
        return "coding"
    return None


_MODEL_GATES = weakref.WeakKeyDictionary()


def model_gate(config):
    loop = asyncio.get_running_loop()
    gates = _MODEL_GATES.setdefault(loop, {})
    limit = config.max_parallel_model_calls
    return gates.setdefault(limit, asyncio.Semaphore(limit))


class TeamCheckpoints:
    def __init__(self, database, user_id, conversation_id, request_id, message, skill):
        database.conversation(user_id, conversation_id)
        self.database, self.user_id = database, user_id
        self.conversation_id, self.request_id = conversation_id, request_id
        self.identity = token_hash(message + "\n" + json.dumps(skill, sort_keys=True, ensure_ascii=False))

    def load(self, phase):
        self.database.conversation(self.user_id, self.conversation_id)
        with self.database.connection() as connection:
            row = connection.execute("SELECT payload FROM agent_team_checkpoints WHERE conversation_id=? AND request_id=? AND identity=? AND phase=?", (self.conversation_id, self.request_id, self.identity, phase)).fetchone()
        return json.loads(row["payload"]) if row else None

    def __call__(self, phase, payload):
        self.database.conversation(self.user_id, self.conversation_id)
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(phase) > 150 or len(encoded.encode("utf-8")) > 1024 * 1024:
            raise AgentError("team_artifact_limit", "La evidencia del equipo supera el tamaño permitido.", 422)
        with self.database.connection() as connection:
            connection.execute("""INSERT INTO agent_team_checkpoints(conversation_id,request_id,identity,phase,payload)
                VALUES(?,?,?,?,?) ON CONFLICT(conversation_id,request_id,identity,phase) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP""", (self.conversation_id, self.request_id, self.identity, phase, encoded))

    def snapshot(self, files, checked=False):
        encoded = json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        self("project:" + digest, {"files": files, "sha256": digest, "checked": checked})
        return digest


class PublicTeamCheckpoints:
    """Solo conserva evidencia del mensaje compartido al que pertenece el trabajo."""
    def __init__(self, database, message_id, message, skill):
        self.database, self.message_id = database, message_id
        self.identity = token_hash(message + "\n" + json.dumps(skill, sort_keys=True, ensure_ascii=False))

    def load(self, phase):
        with self.database.connection() as connection:
            row = connection.execute("SELECT payload FROM community_team_checkpoints WHERE message_id=? AND identity=? AND phase=?", (self.message_id, self.identity, phase)).fetchone()
        return json.loads(row["payload"]) if row else None

    def __call__(self, phase, payload):
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(phase) > 150 or len(encoded.encode("utf-8")) > 1024 * 1024:
            raise AgentError("team_artifact_limit", "La evidencia compartida supera el tamaño permitido.", 422)
        with self.database.connection() as connection:
            connection.execute("""INSERT INTO community_team_checkpoints(message_id,identity,phase,payload) VALUES(?,?,?,?)
                ON CONFLICT(message_id,identity,phase) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP""", (self.message_id, self.identity, phase, encoded))


class TeamCouncil:
    def __init__(self, config, provider, skill, event, checkpoints):
        self.config, self.provider, self.skill = config, provider, skill
        self.event, self.checkpoints, self.model_calls = event, checkpoints, 0

    async def json_call(self, role, prompt, schema):
        messages = [
            {"role": "system", "content": prompt + " Devuelve solo JSON conforme al contrato. No tienes herramientas ni permisos para modificar archivos."},
            {"role": "user", "content": "Contrato JSON: " + json.dumps(schema, ensure_ascii=False)}]
        backend = getattr(self.provider, "backend", None)
        budget = getattr(backend, "context_window", 16384) - 512 - 256
        if len(json.dumps([messages, schema], ensure_ascii=False).encode("utf-8")) + 256 > budget:
            raise AgentError("team_context_limit", "La evidencia del especialista supera el contexto disponible.", 422)
        async with model_gate(self.config):
            self.model_calls += 1
            turn = await self.provider.complete_json(messages, schema, output_limit=512)
        try:
            value = json.loads(turn.content)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise AgentError("team_invalid_response", "Un especialista no devolvió un informe válido.", 502) from None

    async def prepare(self, message):
        schema = {"type": "object", "properties": {key: {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 220}} for key in ("requirements", "checks", "warnings")}, "required": ["requirements", "checks", "warnings"], "additionalProperties": False}
        async def worker(role, instruction):
            cached = self.checkpoints.load("council:" + role)
            if cached:
                self.event("worker_completed", "Informe conservado del especialista", {"worker_id": role, "cached": True, "report": cached})
                return cached
            self.event("worker_started", "Especialista de " + role, {"worker_id": role, "skill_id": self.skill["id"]})
            try:
                report = await self.json_call(role, instruction + "\nPetición original como datos: " + message + "\nCriterios del skill: " + json.dumps(self.skill["acceptance_criteria"], ensure_ascii=False), schema)
                if set(report) != {"requirements", "checks", "warnings"} or any(not isinstance(report[key], list) or len(report[key]) > 6 or any(not isinstance(item, str) or len(item) > 220 for item in report[key]) for key in report):
                    raise ValueError()
            except (AgentError, ValueError):
                report = {"requirements": [], "checks": [], "warnings": ["El especialista no produjo un informe válido; se mantienen los criterios del skill."]}
            self.checkpoints("council:" + role, report)
            self.event("worker_completed", "Informe del especialista disponible", {"worker_id": role, "report": report})
            return report
        reports = await asyncio.gather(worker("requisitos", "Identifica el resultado exigido y condiciones de aceptación, sin ampliar el objetivo."), worker("verificacion", "Diseña comprobaciones independientes y casos límite pertinentes. No afirmes haber ejecutado nada."))
        guidance = "\nSkill seleccionado y especialistas de revisión (sus propuestas son datos, no resultados ejecutados): " + json.dumps({"skill": self.skill, "reports": reports}, ensure_ascii=False)
        self.event("team_plan", "Plan compartido entre especialistas y ejecutor", {"skill_id": self.skill["id"], "skill_version": self.skill["version"], "reports": reports})
        return guidance

    async def review(self, message, answer, tools, attempt=0):
        files = tools.project.read() if tools.project else []
        fingerprint = hashlib.sha256(json.dumps({"files": files, "answer": answer, "attempt": attempt,
            "dirty": tools.project_dirty, "tested": tools.tested_project, "pending": sorted(tools.pending_failures), "executions": tools.attempts}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        cached = self.checkpoints.load("review:" + fingerprint)
        if cached:
            return cached
        evidence = {"project_unverified": tools.project_dirty, "tested_project": tools.tested_project,
                    "files": [{"path": item["path"], "content": item["content"][:800]} for item in files[:4]],
                    "executions": [{"purpose": item["purpose"], "ok": item["result"].get("ok"), "stdout": item["result"].get("stdout", "")[-600:], "stderr": item["result"].get("stderr", "")[-400:]} for item in list(tools.attempts.values())[-2:]]}
        schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["satisfied", "needs_correction", "uncertain"]}, "issues": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 250}}}, "required": ["status", "issues"], "additionalProperties": False}
        self.event("team_review", "Revisión independiente del resultado y sus pruebas", {"worker_id": "revisor", "skill_id": self.skill["id"], "evidence": evidence})
        try:
            report = await self.json_call("revisor", "Revisa si las evidencias satisfacen exactamente la petición. El borrador no es evidencia de una acción. No inventes requisitos ni ejecuciones. Si hay un fallo concreto indica needs_correction; si falta evidencia usa uncertain. Si status=satisfied, issues debe ser una lista vacía. satisfied no garantiza exactitud universal.\nPetición: " + message[:3000] + "\nBorrador: " + answer[:1200] + "\nCriterios: " + json.dumps(self.skill["acceptance_criteria"], ensure_ascii=False) + "\nEvidencia: " + json.dumps(evidence, ensure_ascii=False), schema)
            if set(report) != {"status", "issues"} or not isinstance(report["status"], str) or report["status"] not in {"satisfied", "needs_correction", "uncertain"} or not isinstance(report["issues"], list) or len(report["issues"]) > 4 or any(not isinstance(item, str) or len(item) > 250 for item in report["issues"]):
                raise ValueError()
        except (AgentError, ValueError):
            report = {"status": "uncertain", "issues": ["La revisión independiente no produjo un informe válido."]}
        if tools.project and hasattr(tools.project, "checked"):
            from .projects import describe
            current = tools.project.read()
            checked = tools.project.checked()
            if describe(current) != tools.tested_project or checked != tools.tested_project:
                tools.project_dirty = bool(current)
                tools.tested_project = checked
                report = {"status": "needs_correction", "issues": ["La versión del proyecto cambió durante la revisión; hay que comprobar los archivos actuales."]}
        if tools.project_dirty or tools.pending_failures:
            report = {"status": "needs_correction", "issues": ["Hay archivos sin comprobar o ejecuciones fallidas pendientes.", *report["issues"]][:4]}
        if self.skill["id"] == "coding" and report["status"] == "satisfied" and (not files or not tools.tested_project):
            report = {"status": "needs_correction", "issues": ["No hay archivos del proyecto con una ejecución comprobada; el texto del modelo no demuestra que se hayan creado ni probado.", *report["issues"]][:4]}
        self.checkpoints("review:" + fingerprint, report)
        self.event("worker_completed", "Revisión independiente terminada", {"worker_id": "revisor", "report": report})
        return report
