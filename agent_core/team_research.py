"""Bounded collaboration over independently read, owner-scoped web sources.

The model proposes queries and evidence-backed findings. This controller owns
the tools, concurrency, citations, budgets, and final representation. Search
snippets never qualify as read evidence and cannot be cited by the team.
"""
import asyncio
import copy
import ipaddress
import inspect
import json
import re
import unicodedata
import weakref
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentError
from .retrieval import canonical_url
from .team_control import model_gate


_LOOP_LIMITERS = weakref.WeakKeyDictionary()
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts" / "team_research"


def _normal(value):
    return "".join(char for char in unicodedata.normalize("NFD", value.lower())
                   if unicodedata.category(char) != "Mn")


def web_scope_allowed(message):
    text = _normal(message).strip()
    if (re.search(r"\b(?:no|sin)\s+(?:(?:quiero|necesito|debes|puedes|que)\s+)*(?:bus(?:c|qu)\w*|investig\w*|consult\w*|naveg\w*)\b", text)
            or re.search(r"\b(?:sin internet|no uses internet|no uses la web)\b", text)):
        return False
    private_scope = re.search(r"\b(?:(?:mis?|tus?)\s+(?:documentos|archivos|memoria|historial|notas)|archivos del proyecto|memoria privada|documentos privados)\b", text)
    public_scope = re.search(r"\b(?:internet|web|fuentes publicas|fuentes externas|paginas publicas)\b|https?://", text)
    if private_scope and not public_scope:
        return False
    return True


def research_requested(message, history=(), profile=None):
    """Keep greetings/downloads local; use research for explicit factual lookup."""
    text = _normal(message).strip()
    words = re.findall(r"[a-z0-9]+", text)
    if not words or (len(words) <= 6 and re.match(
            r"^(?:hola|ola|buenas|buenos dias|buenas tardes|buenas noches|gracias|ok|vale|adios)\b", text)):
        return False
    if re.search(r"\b(?:descarga\w*|baja(?:r|me)?|extrae\w*)\b", text) or not web_scope_allowed(message):
        return False
    allowed = getattr(profile, "tools", ()) if profile is not None else ("web_search", "web_read")
    if not {"web_search", "web_read"}.issubset(set(allowed)):
        return False
    if re.search(r"\b(?:bus(?:c|qu)\w*|investiga\w*|averigua\w*|consulta\w*|verifica\w*|contrasta\w*)\b", text):
        return True
    if getattr(profile, "research", False):
        return True
    if re.match(r"^(?:y |tambien |ahora |compara |cual |que hay de )", text):
        previous = next((row.get("content", "") for row in reversed(history)
                         if row.get("role") == "user"), "")
        return bool(previous and research_requested(previous))
    return False


def _bounded(config, name, default, low, high):
    value = getattr(config, name, default)
    if type(value) is not int or not low <= value <= high:
        raise AgentError("team_configuration", "La configuración del equipo supera sus límites.", 503)
    return value


def _limiter(kind, limit):
    """A process-wide limiter per event loop, not one semaphore per request."""
    loop = asyncio.get_running_loop()
    group = _LOOP_LIMITERS.setdefault(loop, {})
    if kind not in group:
        group[kind] = (limit, asyncio.Semaphore(limit))
    configured, semaphore = group[kind]
    if configured != limit:
        # Never silently create a second pool while work uses the first one.
        raise AgentError("team_configuration", "Reinicia el servidor tras cambiar la concurrencia del equipo.", 503)
    return semaphore


def _text(value, maximum, *, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError("invalid text")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise ValueError("control character")
    return value.strip()


def _plain(value):
    """Prevent model/source prose from inventing clickable links or citations."""
    value = re.sub(r"https?://\S+", "[enlace omitido]", value)
    return value.translate(str.maketrans({"[": "(", "]": ")", "<": "", ">": "", "`": ""}))


def _public_readable(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443} or len(url) > 2000
                or any(ord(char) < 32 for char in url)):
            return False
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global and not address.is_reserved and not address.is_multicast
        except ValueError:
            return True
    except (TypeError, ValueError):
        return False


def _same_quote(quote, excerpt):
    return " ".join(quote.split()).casefold() in " ".join(excerpt.split()).casefold()


def _site_filters(task):
    return list(dict.fromkeys(match.rstrip(".,;)").lower()
                             for match in re.findall(r"\bsite:[a-zA-Z0-9][a-zA-Z0-9./_-]*", task)))[:3]


def _search_query(value, required_sites):
    query = _text(value, 300)
    if ("\\" in query or any(token in query for token in ("(?", ".*", "^", "|", "${", "{{"))
            or re.search(r"\?[a-zA-Z_][a-zA-Z0-9_-]{0,25}=", query)):
        raise ValueError("search queries cannot be regex or invented URL templates")
    proposed = _site_filters(query)
    if required_sites:
        if any(site not in required_sites for site in proposed):
            raise ValueError("query changed the requested site")
        missing = [site for site in required_sites if site not in proposed]
        query = " ".join([*missing, query])
    if len(query) > 300:
        raise ValueError("query plus required filters is too long")
    return query


def _fallback_queries(task):
    sites = _site_filters(task)
    text = re.sub(r"\bsite:[a-zA-Z0-9][a-zA-Z0-9./_-]*", " ", task)
    text = re.sub(r"https?://([^\s/?#]+)([^\s?#]*)[^\s]*", r"\1 \2", text)
    stop = set("a al con de del el en es la las lo los para por que un una y o me mi favor "
               "busca buscar busques investiga investigar averigua consulta consultar quiero necesito "
               "puedes podrias documentacion oficial fuentes fuente leidas leida leidos "
               "explica explicar brevemente sirve sirven son como sobre acerca dame muestra informacion".split())
    tokens, seen = [], set()
    for token in re.findall(r"[a-zA-ZÀ-ÿ0-9][a-zA-ZÀ-ÿ0-9_.:+-]*", text):
        token = token.rstrip(".:+-")
        identity = _normal(token)
        if identity and identity not in stop and identity not in seen:
            tokens.append(token)
            seen.add(identity)
    topic = " ".join(tokens) or "información solicitada"
    prefix = " ".join(sites)
    available = 300 - len(prefix) - 32
    topic = topic[:max(40, available)]
    return [" ".join(part for part in (prefix, topic, suffix) if part)
            for suffix in ("documentación oficial", "ejemplos y limitaciones")]


def _tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}}


_STRING = {"type": "string", "minLength": 1, "maxLength": 500}
_EVIDENCE = {"type": "object", "properties": {
    "source_id": {"type": "integer", "minimum": 1, "maximum": 8},
    "quote": {"type": "string", "minLength": 12, "maxLength": 400}},
    "required": ["source_id", "quote"], "additionalProperties": False}
_FINDING = {"type": "object", "properties": {"text": _STRING,
    "evidence": {"type": "array", "minItems": 1, "maxItems": 2, "items": _EVIDENCE}},
    "required": ["text", "evidence"], "additionalProperties": False}


class TeamResearch:
    """Two isolated researchers, serialized LLM access, and a critical synthesis.

    tools_factory(worker_id) must return a fresh Tools instance with this run's
    owner captured. checkpointer(phase, payload) may be sync or async. It only
    receives explicit findings/evidence and execution state, never private
    chain-of-thought. The caller persists the final turn and handles auth.
    """
    def __init__(self, config, provider, tools_factory, event=None, checkpointer=None, skill=None):
        self.config, self.provider, self.tools_factory = config, provider, tools_factory
        self.event = event or (lambda *args: None)
        self.checkpointer = checkpointer
        self.skill = skill or {"id": "research", "version": "1.0.0"}
        if not isinstance(self.skill, dict) or not isinstance(self.skill.get("id"), str) or not isinstance(self.skill.get("version"), str):
            raise AgentError("team_skill", "El skill de investigación necesita identidad y versión.", 503)
        if not {"web_search", "web_read"}.issubset(set(self.skill.get("tools", ["web_search", "web_read"]))):
            raise AgentError("team_skill", "El skill de investigación debe permitir buscar y leer fuentes.", 503)
        self.skill_contract = {key: self.skill[key] for key in ("id", "version", "purpose", "acceptance_criteria", "repair_policy") if key in self.skill}
        if len(json.dumps(self.skill_contract, ensure_ascii=False)) > 4000:
            raise AgentError("team_skill", "El contrato del skill supera el tamaño permitido.", 503)
        self.parallel = _bounded(config, "max_parallel_workers", 2, 1, 4)
        self.model_parallel = _bounded(config, "max_parallel_model_calls", 1, 1, 2)
        self.search_limit = _bounded(config, "max_searches_per_worker", 2, 1, 2)
        self.read_limit = _bounded(config, "max_reads_per_worker", 2, 1, 3)
        self.source_limit = _bounded(config, "max_sources", 6, 2, 8)
        self.source_chars = _bounded(config, "source_chars", 1800, 300, 4000)
        self.call_limit = _bounded(config, "max_model_calls", 5, 1, 8)
        self.repair_limit = _bounded(config, "max_repairs", 1, 0, 1)
        self.timeout = _bounded(config, "timeout_seconds", 600, 10, 1800)
        self.calls, self.tools_used, self.errors = 0, set(), []
        self.workers, self.sources, self.review = [], [], None
        self._tool_instances = []
        self._running = False
        self.reused_phases = []

    async def _checkpoint(self, phase, payload):
        if self.checkpointer:
            result = self.checkpointer("research:" + phase, payload)
            if inspect.isawaitable(result):
                await result

    async def _load(self, phase):
        loader = getattr(self.checkpointer, "load", None)
        if not loader:
            return None
        result = loader("research:" + phase)
        return await result if inspect.isawaitable(result) else result

    def _error(self, stage, code, worker_id=None):
        item = {"stage": stage, "code": str(code)[:100]}
        if worker_id:
            item["worker_id"] = worker_id
        self.errors.append(item)
        return item

    async def _model(self, stage, task, data, schema, history=()):
        if self.calls >= self.call_limit:
            raise AgentError("team_model_budget", "Se agotó el presupuesto de llamadas del equipo.", 422)
        try:
            prompt = (_PROMPTS / (stage + ".md")).read_text("utf-8")
        except OSError:
            raise AgentError("team_skill", "No se pudo cargar el prompt versionado del equipo.") from None
        # Only user messages from this already authorized conversation can be
        # included; shared-channel or cross-owner history is never requested.
        previous = [row["content"][:900] for row in history
                    if row.get("role") == "user" and isinstance(row.get("content"), str)
                    and research_requested(row["content"])
                    and not re.search(r"\b(?:contrasena\w*|password\w*|clave\w*|token|secreto\w*|credencial\w*|bearer|api key)\b|sk-[a-z0-9]{8,}", _normal(row["content"]))][-2:]
        payload = {"request": task, "previous_user_requests": previous, "skill": self.skill_contract, "data": data}
        if hasattr(self.provider, "complete_json"):
            prompt += "\nDevuelve solo un objeto JSON con este contrato: " + json.dumps(schema["function"]["parameters"], ensure_ascii=False)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        backend = getattr(self.provider, "backend", None)
        budget = getattr(backend, "context_window", 16384) - getattr(backend, "max_output_tokens", 1024) - 256
        if hasattr(self.provider, "complete_json"):
            cost = len(json.dumps([messages, schema["function"]["parameters"]], ensure_ascii=False).encode("utf-8"))
        elif hasattr(self.provider, "structured_request"):
            body, _ = self.provider.structured_request(messages, [schema])
            cost = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        else:
            cost = len(json.dumps([messages, schema], ensure_ascii=False).encode("utf-8"))
        if cost > budget:
            payload["previous_user_requests"] = []
            messages[-1]["content"] = json.dumps(payload, ensure_ascii=False)
            if hasattr(self.provider, "complete_json"):
                cost = len(json.dumps([messages, schema["function"]["parameters"]], ensure_ascii=False).encode("utf-8"))
            elif hasattr(self.provider, "structured_request"):
                body, _ = self.provider.structured_request(messages, [schema])
                cost = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
            else:
                cost = len(json.dumps([messages, schema], ensure_ascii=False).encode("utf-8"))
            if cost > budget:
                raise AgentError("team_context_limit", "Los datos del equipo superan el contexto disponible.", 422)
        async with model_gate(self.config):
            # Recheck after waiting: workers share the same run-wide counter.
            if self.calls >= self.call_limit:
                raise AgentError("team_model_budget", "Se agotó el presupuesto de llamadas del equipo.", 422)
            self.calls += 1
            if hasattr(self.provider, "complete_json"):
                turn = await self.provider.complete_json(messages, schema["function"]["parameters"])
                try:
                    result = json.loads(turn.content)
                    if not isinstance(result, dict) or turn.calls:
                        raise ValueError()
                    return result
                except (ValueError, TypeError):
                    raise AgentError("team_invalid_response", "El modelo no devolvió el contrato JSON del equipo.", 502) from None
            turn = await self.provider.structured_action(messages, [schema])
        if len(turn.calls) != 1:
            raise AgentError("team_invalid_response", "El modelo no respetó el contrato del equipo.", 502)
        try:
            function = turn.calls[0]["function"]
            if function["name"] != schema["function"]["name"]:
                raise ValueError()
            value = json.loads(function["arguments"])
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (KeyError, TypeError, ValueError):
            raise AgentError("team_invalid_response", "El modelo no devolvió datos estructurados válidos.", 502) from None

    def _plan_schema(self):
        worker = {"type": "object", "properties": {
            "focus": {"type": "string", "minLength": 1, "maxLength": 200},
            "queries": {"type": "array", "minItems": 1, "maxItems": self.search_limit,
                        "items": {"type": "string", "minLength": 1, "maxLength": 300}}},
                  "required": ["focus", "queries"], "additionalProperties": False}
        return _tool("team_plan", "Dos enfoques complementarios de investigación.",
                     {"workers": {"type": "array", "minItems": 2, "maxItems": 2, "items": worker}}, ["workers"])

    async def _plan(self, task, history):
        restored = await self._load("plan")
        required_sites = _site_filters(task)
        try:
            result = ({"workers": [{"focus": item["focus"], "queries": item["queries"]} for item in restored["workers"]]}
                      if isinstance(restored, dict) and "workers" in restored
                      else await self._model("plan", task, {"required_site_filters": required_sites,
                                                            "safe_queries": _fallback_queries(task)}, self._plan_schema(), history))
            if set(result) != {"workers"} or not isinstance(result["workers"], list) or len(result["workers"]) != 2:
                raise ValueError()
            workers = []
            for index, item in enumerate(result["workers"]):
                if not isinstance(item, dict) or set(item) != {"focus", "queries"}:
                    raise ValueError()
                queries = item["queries"]
                if not isinstance(queries, list) or not 1 <= len(queries) <= self.search_limit:
                    raise ValueError()
                queries = list(dict.fromkeys(_search_query(query, required_sites) for query in queries))
                workers.append({"worker_id": "researcher_" + str(index + 1), "focus": _text(item["focus"], 200), "queries": queries})
            if workers[0]["queries"] == workers[1]["queries"]:
                raise ValueError("identical approaches")
            if restored:
                self.reused_phases.append("plan")
            return workers
        except (AgentError, ValueError, TypeError, KeyError) as error:
            recovered = self._error("plan", error.code if isinstance(error, AgentError) else "team_invalid_plan")
            recovered.update({"resolved": True, "recovery": "deterministic_search_queries"})
            queries = _fallback_queries(task)
            return [{"worker_id": "researcher_1", "focus": "Fuentes primarias y evidencia directa", "queries": [queries[0]]},
                    {"worker_id": "researcher_2", "focus": "Comprobación independiente y limitaciones", "queries": [queries[1]]}]

    def _findings(self, raw, sources, maximum=3):
        if not isinstance(raw, list) or len(raw) > maximum:
            raise ValueError("invalid findings")
        by_id = {source["id"]: source for source in sources}
        findings = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"text", "evidence"}:
                raise ValueError("invalid finding")
            text = _text(item["text"], 500)
            evidence = item["evidence"]
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 2:
                raise ValueError("invalid evidence")
            checked = []
            for piece in evidence:
                if not isinstance(piece, dict) or set(piece) != {"source_id", "quote"}:
                    raise ValueError("invalid citation")
                source_id = piece["source_id"]
                quote = _text(piece["quote"], 400, minimum=12)
                if type(source_id) is not int or source_id not in by_id or not _same_quote(quote, by_id[source_id]["excerpt"]):
                    raise ValueError("citation is not in read source")
                checked.append({"source_id": source_id, "quote": quote})
            findings.append({"text": text, "evidence": checked})
        return findings

    def _analyst_schema(self):
        return _tool("team_findings", "Hallazgos con citas textuales de las páginas leídas.", {
            "findings": {"type": "array", "maxItems": 3, "items": _FINDING},
            "gaps": {"type": "array", "maxItems": 4, "items": _STRING}}, ["findings", "gaps"])

    @staticmethod
    def _gaps(raw):
        if not isinstance(raw, list) or len(raw) > 4:
            raise ValueError("invalid gaps")
        return [_text(item, 500) for item in raw]

    async def _worker(self, plan, task):
        worker_id = plan["worker_id"]
        restored = await self._load(worker_id)
        if isinstance(restored, dict) and restored.get("status") == "complete" and restored.get("worker_id") == worker_id:
            try:
                sources = restored["sources"]
                if (not isinstance(sources, list) or not 1 <= len(sources) <= self.read_limit
                        or any(not _public_readable(source.get("url")) or source.get("retrieval") != "page_extract"
                               or type(source.get("id")) is not int or not isinstance(source.get("excerpt"), str)
                               or not source["excerpt"].strip() or len(source["excerpt"]) > self.source_chars for source in sources)):
                    raise ValueError()
                if len({source["id"] for source in sources}) != len(sources):
                    raise ValueError()
                restored["findings"] = self._findings(restored["findings"], sources)
                restored["gaps"] = self._gaps(restored["gaps"])
                if restored.get("errors") or not restored["findings"]:
                    raise ValueError()
                self.workers.append(restored)
                self.reused_phases.append(worker_id)
                self.tools_used.update({"web_search", "web_read"})
                self.event("worker_completed", "Investigador recuperado del registro", {"worker_id": worker_id, "status": "complete", "sources": len(sources), "reused": True})
                return restored
            except (ValueError, TypeError, KeyError):
                self._error("checkpoint", "team_invalid_checkpoint", worker_id)
        report = {**plan, "status": "running", "sources": [], "findings": [], "gaps": [], "errors": [], "searches": 0, "reads": 0}
        self.workers.append(report)
        self.event("worker_started", "Investigador iniciado", {"worker_id": worker_id, "focus": plan["focus"], "queries": plan["queries"]})
        try:
            tools = self.tools_factory(worker_id)
            if inspect.isawaitable(tools):
                tools = await tools
            if any(tools is existing for existing in self._tool_instances):
                raise AgentError("team_tools_not_isolated", "Cada investigador necesita herramientas propias.", 503)
            self._tool_instances.append(tools)
            # The caller may have seeded links supplied by the current owner.
            # Read those even when the search provider is unavailable; Tools
            # still authorizes each URL before invoking its isolated reader.
            candidates, seen = [], set()
            for source in getattr(tools, "sources", ()):
                if isinstance(source, dict) and source.get("kind") == "web" and _public_readable(source.get("url")):
                    identity = canonical_url(source["url"])
                    if identity not in seen:
                        candidates.append(source)
                        seen.add(identity)
            async with _limiter("workers", self.parallel):
                for query in plan["queries"][:self.search_limit]:
                    self.tools_used.add("web_search")
                    report["searches"] += 1
                    try:
                        found = await tools.search(query)
                        if not isinstance(found, list):
                            raise AgentError("search_invalid_response", "El buscador devolvió datos no válidos.", 502)
                        for source in found:
                            if not isinstance(source, dict) or not _public_readable(source.get("url")):
                                continue
                            identity = canonical_url(source["url"])
                            if identity not in seen:
                                seen.add(identity)
                                candidates.append(source)
                    except AgentError as error:
                        report["errors"].append({"stage": "search", "code": error.code})
                for candidate in candidates[:self.read_limit]:
                    self.tools_used.add("web_read")
                    report["reads"] += 1
                    try:
                        observed = await tools._execute("web_read", {"url": candidate["url"]})
                        if (not isinstance(observed, dict) or observed.get("ok") is not True
                                or not isinstance(observed.get("text"), str) or not observed["text"].strip()):
                            code = observed.get("error", "reader_no_evidence") if isinstance(observed, dict) else "reader_invalid_output"
                            report["errors"].append({"stage": "read", "code": str(code)[:100], "url": candidate["url"]})
                            continue
                        # A reader response cannot replace an authorized URL with
                        # an unrelated one. Existing WebReader never redirects.
                        if observed.get("url", candidate["url"]) != candidate["url"]:
                            report["errors"].append({"stage": "read", "code": "reader_url_mismatch", "url": candidate["url"]})
                            continue
                        text = observed["text"][:self.source_chars]
                        source = {"id": len(report["sources"]) + 1, "kind": "web", "url": candidate["url"],
                                  "title": str(candidate.get("title", "Fuente web"))[:200], "excerpt": text,
                                  "retrieval": "page_extract", "truncated": bool(observed.get("truncated")) or len(text) < len(observed["text"]),
                                  "worker_ids": [worker_id], "read_scope": str(observed.get("scope", "Extracto de la página; no lectura completa."))[:400]}
                        report["sources"].append(source)
                        self.event("source_read", "Fuente leída en contenedor", {"worker_id": worker_id, "url": source["url"], "characters": len(text), "truncated": source["truncated"]})
                    except AgentError as error:
                        report["errors"].append({"stage": "read", "code": error.code, "url": candidate["url"]})
            # No model call is spent analyzing snippets or blocked pages.
            if report["sources"]:
                try:
                    data = {"worker_id": worker_id, "focus": plan["focus"], "sources": report["sources"], "blocked_operations": report["errors"]}
                    raw = await self._model("analyst", task, data, self._analyst_schema())
                    if set(raw) != {"findings", "gaps"}:
                        raise ValueError()
                    report["findings"] = self._findings(raw["findings"], report["sources"])
                    report["gaps"] = self._gaps(raw["gaps"])
                except (AgentError, ValueError, TypeError, KeyError) as error:
                    report["errors"].append({"stage": "analysis", "code": error.code if isinstance(error, AgentError) else "team_invalid_evidence"})
            report["status"] = "complete" if report["findings"] and not report["errors"] else "partial" if report["sources"] else "blocked"
        except AgentError as error:
            report["errors"].append({"stage": "worker", "code": error.code})
            report["status"] = "blocked"
        except Exception:
            # External exceptions may contain credentials or page bodies. Store
            # an allowlisted code only and allow the independent worker to finish.
            report["errors"].append({"stage": "worker", "code": "team_worker_failed"})
            report["status"] = "partial" if report["sources"] else "blocked"
        finally:
            if report["status"] == "running":
                report["status"] = "interrupted"
            await self._checkpoint(worker_id, report)
            self.event("worker_completed", "Investigador finalizado", {"worker_id": worker_id, "status": report["status"], "sources": len(report["sources"]), "findings": len(report["findings"]), "errors": report["errors"]})
        return report

    def _merge(self):
        by_url = {}
        for report in sorted(self.workers, key=lambda item: item["worker_id"]):
            mapping = {}
            for source in report["sources"]:
                identity = canonical_url(source["url"])
                existing = by_url.get(identity)
                if existing is None and len(self.sources) < self.source_limit:
                    existing = {**source, "id": len(self.sources) + 1, "worker_ids": list(source["worker_ids"])}
                    self.sources.append(existing)
                    by_url[identity] = existing
                elif existing is not None:
                    existing["worker_ids"] = sorted(set(existing["worker_ids"] + source["worker_ids"]))
                if existing is not None:
                    mapping[source["id"]] = existing["id"]
            remapped = []
            for finding in report["findings"]:
                if all(piece["source_id"] in mapping for piece in finding["evidence"]):
                    converted = {**finding, "evidence": [{**piece, "source_id": mapping[piece["source_id"]]} for piece in finding["evidence"]]}
                    # A duplicate URL could have returned a different extract.
                    try:
                        remapped.extend(self._findings([converted], self.sources))
                    except ValueError:
                        report["errors"].append({"stage": "exchange", "code": "source_extract_changed"})
            report["shared_findings"] = remapped

    def _review_schema(self):
        contradiction = {"type": "object", "properties": {"text": _STRING,
            "evidence": {"type": "array", "minItems": 2, "maxItems": 2, "items": _EVIDENCE}},
            "required": ["text", "evidence"], "additionalProperties": False}
        return _tool("team_review", "Revisión crítica y síntesis final, ancladas a evidencia leída.", {
            "findings": {"type": "array", "maxItems": 6, "items": _FINDING},
            "contradictions": {"type": "array", "maxItems": 3, "items": contradiction},
            "gaps": {"type": "array", "maxItems": 4, "items": _STRING}}, ["findings", "contradictions", "gaps"])

    def _validate_review(self, raw):
        if set(raw) != {"findings", "contradictions", "gaps"}:
            raise ValueError()
        findings = self._findings(raw["findings"], self.sources, maximum=6)
        contradictions = self._findings(raw["contradictions"], self.sources, maximum=3)
        if any(len(set(piece["source_id"] for piece in item["evidence"])) < 2 for item in contradictions):
            raise ValueError("contradiction needs two read sources")
        return {"findings": findings, "contradictions": contradictions, "gaps": self._gaps(raw["gaps"])}

    async def _critical_review(self, task):
        restored = await self._load("review")
        all_workers_reused = all("researcher_" + str(index) in self.reused_phases for index in (1, 2))
        if all_workers_reused and isinstance(restored, dict) and "findings" in restored:
            try:
                result = self._validate_review(restored)
                self.reused_phases.append("review")
                return result
            except (ValueError, KeyError, TypeError):
                self._error("checkpoint", "team_invalid_checkpoint")
        reports = [{key: report[key] for key in ("worker_id", "focus", "status", "shared_findings", "gaps", "errors")}
                   for report in sorted(self.workers, key=lambda item: item["worker_id"])]
        # Quotes have already been checked against the observed extracts; the
        # reviewer sees them plus the source manifest, not search snippets.
        data = {"sources": [{key: source[key] for key in ("id", "url", "title", "retrieval", "truncated", "read_scope")} for source in self.sources],
                "reports": reports, "controller_errors": self.errors}
        if not any(report["shared_findings"] for report in reports):
            return None
        # El revisor selecciona citas ya comprobadas: no necesita reproducirlas
        # de memoria ni puede inventar otra referencia durante la síntesis.
        citations = []
        for report in reports:
            for finding in report["shared_findings"]:
                for piece in finding["evidence"]:
                    if piece not in citations:
                        citations.append(piece)
        schema = copy.deepcopy(self._review_schema())
        choices = [{"type": "object", "properties": {"source_id": {"type": "integer", "enum": [piece["source_id"]]},
                    "quote": {"type": "string", "enum": [piece["quote"]]}}, "required": ["source_id", "quote"], "additionalProperties": False} for piece in citations[:8]]
        parameters = schema["function"]["parameters"]["properties"]
        for name in ("findings", "contradictions"):
            parameters[name]["items"]["properties"]["evidence"]["items"] = {"anyOf": choices}
        for attempt in range(1 + self.repair_limit):
            self.event("team_review", "Revisión crítica de los hallazgos", {"attempt": attempt + 1, "workers": len(reports), "sources": len(self.sources)})
            try:
                raw = await self._model("review", task, data, schema)
                await self._checkpoint("review_attempt_" + str(attempt + 1), {"proposal": raw})
                result = self._validate_review(raw)
                for error in self.errors:
                    if error["stage"] == "review":
                        error["resolved"] = True
                return result
            except (AgentError, ValueError, TypeError, KeyError) as error:
                code = error.code if isinstance(error, AgentError) else "team_invalid_evidence"
                self._error("review", code)
                if code in {"team_model_budget", "team_context_limit", "provider_timeout", "provider_unavailable"}:
                    break
                data["validation_feedback"] = "La propuesta anterior no respetó el contrato o citó texto no observado. Usa solo las citas exactas de reports, ids válidos, y declara incertidumbre."
        return None

    def _result(self, timed_out=False):
        worker_errors = [{**error, "worker_id": report["worker_id"]}
                         for report in self.workers for error in report["errors"]]
        if self.review is not None:
            findings = self.review["findings"]
            contradictions, gaps = self.review["contradictions"], self.review["gaps"]
        else:
            # Exact observed extracts are a useful partial result, not an
            # unreviewed claim synthesized from a search-engine description.
            findings = [{"text": "Extracto leído: «" + source["excerpt"][:320] + "»",
                         "evidence": [{"source_id": source["id"], "quote": source["excerpt"][:320]}]}
                        for source in self.sources[:3]]
            contradictions, gaps = [], []
        status = "complete" if (self.review is not None and findings and len(self.sources) >= 2 and not timed_out
                  and not contradictions and not gaps and not any(not error.get("resolved") for error in self.errors) and not worker_errors
                  and len(self.workers) == 2 and all(report["status"] == "complete" for report in self.workers)) else "partial" if self.sources else "blocked"
        lines = []
        if status == "blocked":
            lines.append("No pude verificar la respuesta con páginas leídas. La búsqueda o el lector encontraron un bloqueo; no voy a presentar fragmentos del buscador como hechos comprobados.")
        else:
            if status != "complete":
                lines.append("La investigación quedó parcial; estos son los resultados respaldados por extractos que sí se pudieron leer.")
            if not findings:
                lines.append("Se leyeron fuentes, pero no se obtuvieron conclusiones respaldadas para esta petición.")
            for finding in findings:
                ids = list(dict.fromkeys(piece["source_id"] for piece in finding["evidence"]))
                citations = " ".join("[" + str(source_id) + "]" for source_id in ids)
                lines.append(_plain(finding["text"]) + " " + citations)
            for item in contradictions:
                ids = sorted(set(piece["source_id"] for piece in item["evidence"]))
                lines.append("Contradicción pendiente: " + _plain(item["text"]) + " " + " ".join("[" + str(source_id) + "]" for source_id in ids))
            for gap in gaps:
                lines.append("Pendiente de verificar: " + _plain(gap))
            if self.review is None:
                lines.append("La revisión crítica no se completó; los extractos anteriores no constituyen una respuesta final verificada.")
            if worker_errors:
                lines.append("Parte de las consultas o lecturas falló; la cobertura es limitada.")
            if len(self.sources) == 1:
                lines.append("Solo se pudo leer una fuente distinta; dos investigadores consultando la misma página no constituyen contraste independiente.")
            if any(source["truncated"] for source in self.sources):
                lines.append("Se contrastaron extractos limitados, no el contenido completo de todas las páginas.")
        if timed_out:
            lines.append("Se agotó el tiempo asignado al equipo.")
        team = {"status": status, "skill_id": self.skill["id"], "skill_version": self.skill["version"],
                "workers": sorted(self.workers, key=lambda item: item["worker_id"]), "reports": self.review,
                "errors": self.errors + worker_errors, "timed_out": timed_out,
                "source_count": len(self.sources), "model_calls": self.calls,
                "reused_phases": list(self.reused_phases),
                "verification_scope": "Citas y extractos observados; revisión por modelo, sin garantía de verdad universal."}
        return {"answer": "\n\n".join(lines), "sources": self.sources, "model_calls": self.calls,
                "tools_used": sorted(self.tools_used), "review": "team_sources_reviewed" if status == "complete" else "team_partial" if self.sources else "team_blocked", "team": team}

    async def run(self, message, history=()):
        if self._running or self.workers:
            raise AgentError("team_run_reused", "Crea un equipo nuevo para cada turno.", 422)
        self._running = True
        timed_out = False
        try:
            async with asyncio.timeout(self.timeout):
                plans = await self._plan(message, history)
                await self._checkpoint("plan", {"workers": plans, "skill_id": self.skill["id"], "skill_version": self.skill["version"]})
                self.event("team_plan", "Plan compartido entre investigadores", {"skill_id": self.skill["id"], "skill_version": self.skill["version"], "workers": plans})
                await asyncio.gather(*(self._worker(plan, message) for plan in plans))
                self._merge()
                await self._checkpoint("exchange", {"sources": self.sources, "reports": self.workers})
                self.review = await self._critical_review(message)
                await self._checkpoint("review", self.review or {"status": "not_completed"})
        except TimeoutError:
            timed_out = True
            self._error("run", "team_timeout")
            if not self.sources:
                self._merge()
        finally:
            self._running = False
        result = self._result(timed_out)
        await self._checkpoint("result", result)
        self.event("team_completed", "Equipo de investigación finalizado", {"status": result["team"]["status"], "model_calls": self.calls, "sources": len(self.sources), "errors": result["team"]["errors"]})
        return result
