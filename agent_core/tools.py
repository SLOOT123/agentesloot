import ast
import ipaddress
import json
import math
import operator
import os
import re
import uuid
from urllib.parse import urlsplit

from .config import AgentError, validate_server_url
from .providers import JsonHTTP
from .config import SandboxConfig
from .sandbox import SandboxRunner
from .relations import research_graph
from .projects import ProjectFiles, describe, validate_files
from .web_reader import WebReader
from .retrieval import HYBRID_METHOD, rank_sources
from .task_graph import TaskGraph
from .downloads import Downloader, DownloadStore, download_tools
from .tool_catalog import discover


def tool_schema(name, description, parameter):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {parameter: {"type": "string"}},
                           "required": [parameter], "additionalProperties": False}}}


SCHEMAS = {
    "tool_discover": tool_schema('tool_discover', 'Consulta capacidades permitidas, disponibilidad y documentación para cambiar de estrategia.', 'purpose'),
    "task_plan": {"type": "function", "function": {
        "name": "task_plan", "description": "Registra una vez el plan de acciones del turno. Dependencias sin ciclos. El servidor completa pasos solo al observar herramientas reales; las escrituras necesitan una ejecución posterior.",
        "parameters": {"type": "object", "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 5,
            "items": {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,23}$"}, "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "tool": {"type": "string", "enum": ["web_search", "web_read", "project_write", "project_read", "sandbox_exec", "calculator", "memory_search", "research_graph", "download_tools", "download_url", "attachment_read", "reference_read", "tool_discover"]},
                "depends_on": {"type": "array", "maxItems": 6, "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,23}$"}}},
                "required": ["id", "title", "tool", "depends_on"], "additionalProperties": False}}},
            "required": ["steps"], "additionalProperties": False}}},
    "calculator": tool_schema("calculator", "Calcula aritmética básica sin ejecutar código.", "expression"),
    "memory_search": tool_schema("memory_search", "Busca en la memoria documental del usuario actual.", "query"),
    "web_search": tool_schema("web_search", "Busca fragmentos web con enlaces y títulos; no lee las páginas completas.", "query"),
    "web_read": tool_schema("web_read", "Lee un enlace HTTPS dado por el usuario, devuelto por web_search o encontrado en una página leída. Devuelve texto y enlaces. Lector fijo en Docker, robots.txt, sin JavaScript.", "url"),
    "download_tools": {"type": "function", "function": {"name": "download_tools", "description": "Detecta las herramientas instaladas para descargar archivos, extraer medios y convertir a MP3. download_url selecciona y reintenta métodos automáticamente.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    "download_url": {"type": "function", "function": {"name": "download_url", "description": "Descarga en segundo plano un enlace del usuario o fuente consultada. audio prepara MP3; video conserva vídeo y sonido; pdf encuentra y verifica un PDF ofrecido por la página; file conserva archivos compatibles, incluidos documentos. Analiza enlaces e incrustaciones y elige herramientas. queued/running es en proceso; solo complete incluye archivo comprobado. No requiere título, project_write ni sandbox_exec.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "mode": {"type": "string", "enum": ["audio", "video", "file", "pdf"]}}, "required": ["url", "mode"], "additionalProperties": False}}},
    "sandbox_exec": {"type": "function", "function": {
        "name": "sandbox_exec", "description": "Ejecuta Python o sh en Docker. project=true incorpora los archivos privados guardados, en /tmp/project; sus cambios temporales no se guardan. Sin red ni archivos del equipo. Corrige con repair_of.",
        "parameters": {"type": "object", "properties": {
            "language": {"type": "string", "enum": ["python", "sh"], "description": "python para instrucciones Python; sh para comandos de terminal como python -B archivo.py."},
            "code": {"type": "string", "description": "Código del lenguaje elegido. Para ejecutar python -B archivo.py, selecciona language=sh."}, "purpose": {"type": "string"}, "project": {"type": "boolean"},
            "repair_of": {"type": "string", "description": "attempt_id fallido del mismo turno; omite o usa vacío para un script nuevo."}},
            "required": ["language", "code", "purpose"], "additionalProperties": False}}},
    "project_write": {"type": "function", "function": {
        "name": "project_write", "description": "Guarda archivos privados de texto o código; no descarga música ni guarda audio, vídeo o imágenes binarias. Argumentos exactos: {\"files\":[{\"path\":\"saludo.py\",\"content\":\"print(2+2)\\n\"}]}. files es una lista; path y content son texto. No ejecuta ni publica.",
        "parameters": {"type": "object", "properties": {"files": {"type": "array", "minItems": 1, "maxItems": 32,
            "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"], "additionalProperties": False}}}, "required": ["files"], "additionalProperties": False}}},
    "project_read": {"type": "function", "function": {
        "name": "project_read", "description": "Lista archivos con path vacío. Lee un archivo por fragmentos de 1000 caracteres usando offset (predeterminado 0); next_offset indica si queda texto.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "maximum": 16000}}, "required": ["path"], "additionalProperties": False}}},
    "research_graph": {"type": "function", "function": {
        "name": "research_graph", "description": "Relaciona las fuentes ya consultadas por semejanza textual; calcula componentes y ciclos del grafo, sin inferir causalidad.",
        "parameters": {"type": "object", "properties": {"min_similarity": {"type": "number", "minimum": .05, "maximum": 1}}, "additionalProperties": False}}},
    "attachment_read": {"type": "function", "function": {
        "name": "attachment_read", "description": "Lee archivos que el usuario subió a ESTA conversación. attachment_id vacío lista archivos. Lee 1800 caracteres desde offset; next_offset indica continuación. No ejecuta su contenido.",
        "parameters": {"type": "object", "properties": {"attachment_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "maximum": 200000}}, "required": ["attachment_id"], "additionalProperties": False}}},
    "reference_read": {"type": "function", "function": {
        "name": "reference_read", "description": "Consulta MD enlazados al perfil. reference_id vacío lista cuatro documentos desde offset. Con un identificador lee 1000 caracteres desde offset; next_offset permite continuar. No ejecuta ni activa instrucciones del documento.",
        "parameters": {"type": "object", "properties": {"reference_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}, "required": ["reference_id"], "additionalProperties": False}}},
}


def calculate(expression):
    if len(expression) > 200:
        raise ValueError("Expresión demasiado larga")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 40:
        raise ValueError("Demasiadas operaciones")
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.Mod: operator.mod}

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            value = operations[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("Solo se permiten números y + - * / %")
        if not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError("Resultado fuera de límite")
        return value
    return visit(tree)


def public_source_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".localhost")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


class Tools:
    def __init__(self, database, user_id, timeout, transport=None, *, sandbox=None, downloads=None, event=None, learning=False, max_repairs=2, runner=None, conversation_id=None, task_planning=False, download_jobs=None, request_id=""):
        self.database, self.user_id = database, user_id
        self.http = JsonHTTP(min(timeout, 12), transport)
        self.sources = []
        self.source_limit = 8
        self.research_delegate = None
        self.runner = runner or SandboxRunner(sandbox or SandboxConfig())
        self.reader = WebReader(sandbox or SandboxConfig())
        self.event = event or (lambda *args: None)
        self.runner.event = self.event
        self.reader.runner.event = self.event
        self.learning = learning
        self.max_repairs, self.repairs = max_repairs, 0
        self.attempts, self.pending_failures, self.lesson_candidates = {}, set(), []
        self.project = ProjectFiles(database, user_id, conversation_id) if conversation_id else None
        self.task_graph = TaskGraph() if task_planning else None
        current = self.project.read() if self.project else []
        self.tested_project = self.project.checked() if self.project else []
        self.project_dirty = bool(current) and self.tested_project != describe(current)
        self.downloader = Downloader(DownloadStore(database, user_id, conversation_id), downloads, self.event) if conversation_id else None
        self.download_failure = None
        self.download_results = {}
        self.download_jobs, self.conversation_id, self.request_id = download_jobs, conversation_id, request_id
        self.attachments = None
        self.references = None
        self.catalog_allowed = []

    def discover_tools(self, purpose, allowed):
        descriptions = {name: schema['function']['description'] for name, schema in SCHEMAS.items()}
        config = getattr(self.runner, 'config', None)
        downloads = getattr(self.downloader, 'config', None)
        result = discover(allowed, descriptions, purpose, sandbox_enabled=bool(getattr(config, 'enabled', False)),
                          downloads_enabled=bool(getattr(downloads, 'enabled', False)))
        self.event('tool_discovery', 'Herramientas disponibles para el objetivo', result)
        return result

    def user_links(self, text):
        for match in re.findall(r'https?://[^\s<>\[\]"\']+', text):
            url = match.rstrip(".,;!")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            if len(url) <= 2000 and public_source_url(url):
                self.add_sources([{"kind": "web", "url": url, "title": "Enlace del usuario", "excerpt": "Enlace proporcionado en esta conversación; aún no leído."}])

    def add_sources(self, sources):
        result = []
        for source in sources:
            identity = source.get("reference_id") or source.get("attachment_id") or source.get("document_id") or source.get("lesson_id") or source.get("url")
            existing = next((s for s in self.sources if (s.get("reference_id") or s.get("attachment_id") or s.get("document_id") or s.get("lesson_id") or s.get("url")) == identity), None)
            if existing is None and len(self.sources) < self.source_limit:
                existing = {**source, "id": len(self.sources) + 1}
                self.sources.append(existing)
            if existing:
                result.append(existing)
        return result

    def memory(self, query):
        found = self.database.search_documents(self.user_id, query)
        if self.learning:
            found += self.database.search_lessons(self.user_id, query)
        return self.add_sources(found)

    async def script(self, arguments):
        if set(arguments) - {"language", "code", "purpose", "repair_of", "project"} or not {"language", "code", "purpose"} <= set(arguments):
            raise AgentError("invalid_tool_arguments", "El script necesita language, code y purpose; repair_of es opcional.", 502)
        language, code, purpose = arguments["language"], arguments["code"], arguments["purpose"]
        parent_id = arguments.get("repair_of", "")
        project = arguments.get("project", False)
        if (language not in {"python", "sh"} or not isinstance(code, str) or not code.strip() or len(code) > 8000
                or not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 160 or not isinstance(parent_id, str) or type(project) is not bool):
            raise AgentError("invalid_tool_arguments", "Los datos del script no son válidos o superan los límites.", 502)
        parent = self.attempts.get(parent_id) if parent_id else None
        if parent_id:
            if not parent or parent_id not in self.pending_failures or parent["result"].get("ok") or parent["language"] != language or parent.get("project", False) != project:
                raise AgentError("invalid_repair", "Solo se puede corregir un intento fallido de este turno y del mismo lenguaje.", 502)
            if self.repairs >= self.max_repairs:
                return {"ok": False, "error": "repair_limit", "message": "Se agotó el límite de correcciones de este turno."}
            self.repairs += 1
        files = None
        if project:
            if not self.project:
                raise AgentError("project_unavailable", "La ejecución no tiene un proyecto privado asociado.", 422)
            files = self.project.read()
            if not files:
                raise AgentError("empty_project", "Guarda los archivos con project_write antes de ejecutarlos.", 422)
        attempt_id = str(uuid.uuid4())
        self.event("script", "Corrección en contenedor" if parent else "Script en contenedor",
                   {"attempt_id": attempt_id, "repair_of": parent_id, "language": language, "purpose": purpose, "code": code,
                    "project_files": describe(files) if files else []})
        result = await self.runner.run(language, code, files=files) if project else await self.runner.run(language, code)
        self.attempts[attempt_id] = {"parent": parent_id, "language": language, "code": code, "purpose": purpose, "result": result, "project": project}
        if result.get("ok"):
            if project:
                self.project.mark_checked(files)
                self.project_dirty = False
                self.tested_project = describe(files)
            root = parent
            cursor = parent_id
            while cursor:
                self.pending_failures.discard(cursor)
                root = self.attempts[cursor]
                cursor = root["parent"]
            if parent and self.learning and root["result"].get("exit_code") not in {None, 0, 125}:
                self.lesson_candidates.append({"attempt_id": attempt_id, "purpose": purpose, "language": language,
                    "error": (root["result"].get("stderr") or root["result"].get("error") or "Ejecución fallida")[-1000:],
                    "code": code, "output": result.get("stdout", "")[:1200]})
        else:
            self.pending_failures.add(attempt_id)
        return {**result, "attempt_id": attempt_id, "repair_of": parent_id}

    async def search(self, query):
        base = os.environ.get("SEARXNG_URL", "").strip()
        if not base:
            raise AgentError("search_not_configured", "La búsqueda web necesita SEARXNG_URL en el servidor y SearXNG iniciado. Puedes continuar una conversación sin búsqueda mientras lo configuras.")
        try:
            base = validate_server_url(base)
        except ValueError:
            raise AgentError("search_not_configured", "SEARXNG_URL no es una URL válida de servidor.") from None
        try:
            # Algunos motores rechazan site: o nombres de API con punto.
            # El dominio se impone sobre los resultados, no se deja a criterio del motor.
            upstream_query = re.sub(r"(?<![-\w])site:([a-z0-9.-]+)", r"\1", query, flags=re.I)
            upstream_query = re.sub(r"\b([A-Za-z_]\w*)\.([A-Z]\w*)\b", r"\1 \2", upstream_query)
            response = await self.http.request("GET", base + "/search", params={"q": upstream_query[:400], "format": "json"})
        except AgentError as error:
            if error.code == "provider_auth":
                raise AgentError("search_access_denied", "SearXNG rechazó el acceso. Comprueba que permite formato JSON; no es la clave del modelo.", 502) from None
            raise AgentError("search_unavailable", "No se pudo consultar SearXNG. Comprueba su dirección, disponibilidad y límites.", error.status) from None
        results = response.get("results", [])
        if not isinstance(results, list):
            raise AgentError("search_invalid_response", "El buscador devolvió un formato inesperado.", 502)
        if not results and response.get("unresponsive_engines"):
            self.event("search_status", "Los motores de búsqueda no están disponibles", {"status": "blocked", "scope": "El buscador informó límites o verificaciones de acceso; no se obtuvieron resultados."})
            raise AgentError("search_engines_blocked", "Los motores del buscador están limitados o exigen verificación; no se obtuvieron resultados.", 503)
        domains = re.findall(r"(?<![-\w])site:([a-z0-9.-]+)", query, re.I)
        sources = []
        for item in results[:20]:
            if not isinstance(item, dict):
                continue
            url = item.get("url", "")
            if isinstance(url, str) and public_source_url(url):
                host = (urlsplit(url).hostname or "").lower()
                if domains and not any(host == domain.lower() or host.endswith("." + domain.lower()) for domain in domains):
                    continue
                sources.append({"kind": "web", "url": url[:2000], "title": str(item.get("title", "Fuente web"))[:200],
                                "excerpt": str(item.get("content", ""))[:1400], "query": query[:400]})
        selected = rank_sources(query, sources)
        added = self.add_sources(selected)
        self.event("retrieval", "Fuentes clasificadas por pertinencia", {
            "query": query[:400], "candidates": len(sources), "returned": len(added),
            "upstream_query": upstream_query[:400],
            "method": HYBRID_METHOD, "sources": added,
            "weights": {"bm25_normalized": .5, "tfidf_cosine": .35, "title_coverage": .15},
            "scope": "Clasificación léxica y vectorial con diversidad de dominios; no valida hechos."})
        return added

    async def execute(self, name, arguments, allowed):
        if name not in allowed or name not in SCHEMAS:
            raise AgentError("tool_not_allowed", "El modelo intentó usar una herramienta que este perfil no permite.", 502)
        if not isinstance(arguments, dict):
            raise AgentError("invalid_tool_arguments", "Los argumentos deben ser un objeto JSON.", 502)
        if name == "task_plan":
            if not self.task_graph or set(arguments) != {"steps"}:
                raise AgentError("invalid_plan", "El perfil debe habilitar planificación y recibir steps.", 422)
            graph = self.task_graph.plan(arguments["steps"], allowed)
            self.event("task_graph", "Plan de acciones registrado", {"task_graph": graph})
            return {"task_graph": graph}
        self.catalog_allowed = list(allowed)
        planned = self.task_graph and any(node['tool'] == name for node in self.task_graph.nodes.values())
        key = self.task_graph.before(name) if self.task_graph and (name != 'tool_discover' or planned) else None
        try:
            result = await self._execute(name, arguments)
        except AgentError as error:
            if key:
                self.task_graph.record(key, {"ok": False, "error": error.code})
                self.event("task_graph", "Paso pendiente de corrección", {"task_graph": self.task_graph.snapshot()})
            raise
        if key:
            self.task_graph.record(key, result)
            self.event("task_graph", "Estado del plan actualizado", {"task_graph": self.task_graph.snapshot()})
        return result

    async def _execute(self, name, arguments):
        if name == 'tool_discover':
            if set(arguments) != {'purpose'} or not isinstance(arguments['purpose'], str) or not 1 <= len(arguments['purpose'].strip()) <= 500:
                raise AgentError('invalid_tool_arguments', 'tool_discover necesita purpose de uno a 500 caracteres.', 422)
            return self.discover_tools(arguments['purpose'], self.catalog_allowed)
        if name == 'reference_read':
            if self.references is None:
                raise AgentError('references_unavailable', 'Este perfil no tiene documentos enlazados.', 422)
            if set(arguments) - {'reference_id', 'offset'} or 'reference_id' not in arguments:
                raise AgentError('invalid_tool_arguments', 'Envía reference_id y offset opcional.', 422)
            result = self.references.read(arguments['reference_id'], arguments.get('offset', 0))
            if result.get('reference_id'):
                added = self.add_sources([self.references.source(result['reference_id'], result['text'], result['offset'])])
                if added:
                    added[0].update(excerpt=result['text'], offset=result['offset'])
                    result['source_id'] = added[0]['id']
            return result
        if name == "attachment_read":
            if not self.attachments: raise AgentError("attachments_unavailable", "No hay archivos adjuntos en esta conversación.", 422)
            if (set(arguments) - {"attachment_id", "offset"} or not isinstance(arguments.get("attachment_id"), str)
                    or len(arguments["attachment_id"]) > 36 or type(arguments.get("offset", 0)) is not int):
                raise AgentError("invalid_tool_arguments", "Envía attachment_id y offset opcional.", 422)
            if not arguments["attachment_id"]: return {"files": self.attachments.list()}
            result = self.attachments.read(arguments["attachment_id"], arguments.get("offset", 0))
            sources = self.add_sources([{"attachment_id": result["id"], "kind": "attachment", "title": result["name"], "excerpt": result["text"], "retrieval": "file_extract", "url": f"/api/agent/conversations/{self.conversation_id}/attachments/{result['id']}/content"}])
            if sources and result["text"]:
                source = sources[0]
                source["excerpt"] = ((source["excerpt"][:1000] + "\n") if source.get("retrieval") == "vision_model" else "") + result["text"]
                source["offset"] = arguments.get("offset", 0)
            return {**result, "source_id": sources[0]["id"] if sources else None}
        if name == "download_tools":
            if arguments:
                raise AgentError("invalid_tool_arguments", "download_tools recibe un objeto vacío.", 422)
            return download_tools()
        if name == "download_url":
            if (set(arguments) != {"url", "mode"} or not isinstance(arguments["url"], str)
                    or not isinstance(arguments["mode"], str) or arguments["mode"] not in {"audio", "video", "file", "pdf"}):
                raise AgentError("invalid_tool_arguments", "download_url necesita url y mode (audio, video, file o pdf).", 422)
            if not any(item.get("kind") == "web" and item.get("url") == arguments["url"] for item in self.sources):
                raise AgentError("source_not_consulted", "Usa un enlace dado por el usuario o recuperado con las herramientas web.", 422)
            if not self.downloader:
                raise AgentError("download_unavailable", "La descarga necesita una conversación privada.", 422)
            if self.download_jobs:
                return await self.download_jobs.submit(self.user_id, self.conversation_id, arguments["url"], arguments["mode"], self.request_id)
            identity = (arguments["url"], arguments["mode"])
            previous = self.download_results.get(identity)
            if previous is not None:
                self.download_failure = previous if not previous.get("ok") else None
                return {**previous, "reused": True}
            result = await self.downloader.run(*identity)
            if result.get("ok") or result.get("error") in {"site_access_required", "site_rate_limited", "protected_media"}:
                self.download_results[identity] = result
            self.download_failure = result if not result.get("ok") else None
            return result
        if name == "sandbox_exec":
            if self.task_graph and self.project_dirty and arguments.get("project") is not True:
                raise AgentError("project_test_required", "Los archivos guardados necesitan sandbox_exec con project=true; ejecuta esos archivos, no una copia independiente.", 422)
            return await self.script(arguments)
        if name == "web_read":
            if set(arguments) != {"url"} or not isinstance(arguments["url"], str) or len(arguments["url"]) > 2000:
                raise AgentError("invalid_tool_arguments", "Envía una URL de hasta 2000 caracteres.", 422)
            source = next((item for item in self.sources if item.get("kind") == "web" and item.get("url") == arguments["url"]), None)
            if source is None:
                raise AgentError("source_not_consulted", "Usa un enlace dado por el usuario, devuelto por web_search o encontrado en una página ya leída.", 422)
            self.reader.query = source.get("query", "")[:500]
            result = await self.reader.read(arguments["url"])
            if result.get("ok"):
                source.update({"excerpt": result["text"], "retrieval": "page_extract", "truncated": result["truncated"]})
                self.add_sources([{"kind": "web", "url": item["url"], "title": item.get("title", "Enlace de la página"), "excerpt": "Enlace encontrado en " + arguments["url"]}
                                  for item in result.get("links", []) if public_source_url(item.get("url", ""))])
                return {**result, "source_id": source["id"]}
            return result
        if name in {"project_write", "project_read"}:
            if not self.project:
                raise AgentError("project_unavailable", "No hay conversación asociada al proyecto.", 422)
            if name == "project_write":
                if set(arguments) != {"files"}:
                    raise AgentError("invalid_project", 'Argumentos de project_write: {"files":[{"path":"saludo.py","content":"print(2+2)\\n"}]}. No envíes language, project ni code a project_write; esos campos pertenecen a sandbox_exec.', 422)
                checked = validate_files(arguments["files"])
                if self.task_graph:
                    for item in checked:
                        if item["path"].endswith(".py"):
                            try:
                                ast.parse(item["content"], filename=item["path"])
                            except (SyntaxError, ValueError, RecursionError) as error:
                                detail = error.msg if isinstance(error, SyntaxError) else "estructura no válida"
                                raise AgentError("python_syntax", "No se guardó el lote. " + item["path"] + ": " + detail + ". Reenvía files con código Python completo, incluidas las comillas y paréntesis de cierre.", 422) from None
                written = self.project.write(checked)
                self.project_dirty = True
                return {"files": written, "validation": "Archivos guardados; todavía no se ejecutaron ni comprobaron."}
            offset = arguments.get("offset", 0)
            if (set(arguments) - {"path", "offset"} or "path" not in arguments or not isinstance(arguments["path"], str) or len(arguments["path"]) > 120
                    or type(offset) is not int or not 0 <= offset <= 16000):
                raise AgentError("invalid_project", "Envía path vacío para listar, o una ruta para leer.", 422)
            files = self.project.read()
            if not arguments["path"]:
                return {"files": describe(files)}
            match = next((item for item in files if item["path"] == arguments["path"]), None)
            if match is None:
                raise AgentError("project_file_missing", "El archivo no existe en esta conversación.", 404)
            text = match["content"]
            return {"file": {"path": match["path"], "content": text[offset:offset + 1000], "total_characters": len(text),
                             "offset": offset, "next_offset": offset + 1000 if offset + 1000 < len(text) else None}}
        if name == "research_graph":
            threshold = arguments.get("min_similarity", .15)
            if set(arguments) - {"min_similarity"} or type(threshold) not in {int, float} or not math.isfinite(threshold) or not .05 <= threshold <= 1:
                raise AgentError("invalid_tool_arguments", "El umbral debe estar entre 0.05 y 1.", 502)
            return {"graph": research_graph(self.sources, threshold)}
        parameter = "expression" if name == "calculator" else "query"
        if set(arguments) != {parameter} or not isinstance(arguments[parameter], str) or not arguments[parameter].strip() or len(arguments[parameter]) > 500:
            raise AgentError("invalid_tool_arguments", "La herramienta recibió argumentos inválidos.", 502)
        try:
            if name == "calculator":
                return {"result": calculate(arguments[parameter])}
            if name == "memory_search":
                return {"sources": self.memory(arguments[parameter])}
            if name == "web_search" and self.research_delegate:
                return await self.research_delegate(arguments[parameter])
            return {"sources": await self.search(arguments[parameter])}
        except (ValueError, SyntaxError, ArithmeticError, RecursionError):
            return {"error": "La operación no es válida o supera los límites permitidos."}
