"""DAG de acciones observadas. Valida dependencias, no la verdad del resultado."""
import re
from graphlib import CycleError, TopologicalSorter

from .config import AgentError


class TaskGraph:
    def __init__(self):
        self.nodes = {}

    def plan(self, steps, allowed):
        if self.nodes:
            raise AgentError("plan_exists", "El plan de este turno ya existe; completa o reintenta sus pasos.", 422)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
            raise AgentError("invalid_plan", "El plan necesita entre uno y cinco pasos para reservar la respuesta y revisión final.", 422)
        nodes = {}
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"id", "title", "tool", "depends_on"}:
                raise AgentError("invalid_plan", "Cada paso necesita id, title, tool y depends_on.", 422)
            key, title, tool, deps = (step[k] for k in ("id", "title", "tool", "depends_on"))
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", key) or key in nodes
                    or not isinstance(title, str) or not title.strip() or len(title) > 120
                    or not isinstance(tool, str) or tool not in allowed or tool == "task_plan"
                    or not isinstance(deps, list) or len(deps) > 6 or any(not isinstance(d, str) for d in deps)
                    or len(set(deps)) != len(deps)):
                raise AgentError("invalid_plan", "Revisa identificadores únicos, herramientas permitidas y dependencias.", 422)
            nodes[key] = {**step, "depends_on": list(deps), "status": "pending", "attempts": 0}
        if any(dep not in nodes for node in nodes.values() for dep in node["depends_on"]):
            raise AgentError("invalid_plan", "Una dependencia apunta a un paso inexistente.", 422)
        try:
            order = list(TopologicalSorter({key: node["depends_on"] for key, node in nodes.items()}).static_order())
        except CycleError:
            raise AgentError("cyclic_plan", "El plan contiene un ciclo; usa dependencias sin ciclos.", 422) from None
        # Guardar archivos debe preceder a una comprobación real en el plan.
        def ancestors(key):
            return {dep for d in nodes[key]["depends_on"] for dep in ({d} | ancestors(d))}
        for key, node in nodes.items():
            if node["tool"] == "project_write" and not any(
                    n["tool"] == "sandbox_exec" and key in ancestors(k) for k, n in nodes.items()):
                raise AgentError("untested_plan", "Añade sandbox_exec después de project_write para comprobar los archivos.", 422)
        self.nodes = {key: nodes[key] for key in order}
        return self.snapshot()

    def before(self, tool):
        if not self.nodes:
            raise AgentError("plan_required", "Crea primero el plan mediante task_plan.", 422)
        ready = [key for key, n in self.nodes.items() if n["tool"] == tool
                 and all(self.nodes[d]["status"] == "observed" for d in n["depends_on"])]
        key = next((k for k in ready if self.nodes[k]["status"] != "observed"), None)
        if key is None:
            # Permite reparar un archivo o repetir una búsqueda, invalidando lo que dependía de ellos.
            key = next(iter(ready), None)
        if key is None:
            raise AgentError("dependency_pending", "La herramienta no está lista en el plan. Completa sus dependencias.", 422)
        affected = {key}
        for candidate, node in self.nodes.items():
            if candidate in affected or affected.intersection(node["depends_on"]):
                affected.add(candidate)
                node["status"] = "pending"
                node.pop("evidence", None)
        return key

    def record(self, key, result):
        node = self.nodes[key]
        success = result.get("ok") is not False and not result.get("error")
        if node["tool"] == "web_search" and not result.get("sources"):
            success = False
        node["status"] = "observed" if success else "failed"
        node["attempts"] += 1
        node["evidence"] = {key: result[key] for key in ("attempt_id", "exit_code", "error", "source_id", "result") if key in result}
        if "sources" in result:
            node["evidence"]["source_ids"] = [s["id"] for s in result["sources"] if isinstance(s, dict) and "id" in s]
        if "files" in result:
            node["evidence"]["files"] = result["files"]
        if result.get("download"):
            node["evidence"]["download"] = {key: result["download"][key] for key in ("id", "filename", "bytes", "sha256")}
        if result.get("job"):
            node["evidence"]["download_job"] = {"id": result["job"]["id"], "status": result["job"]["status"]}

    @property
    def pending(self):
        return not self.nodes or any(n["status"] != "observed" for n in self.nodes.values())

    def available_tools(self):
        ready = [n for n in self.nodes.values() if all(self.nodes[d]["status"] == "observed" for d in n["depends_on"])]
        pending = [n for n in ready if n["status"] != "observed"]
        # Tras un fallo conserva también las herramientas previas para poder reparar archivos.
        choices = ready if any(n["status"] == "failed" for n in ready) or not pending else pending
        return list(dict.fromkeys(n["tool"] for n in choices))

    def snapshot(self):
        return {"nodes": [dict(n) for n in self.nodes.values()],
                "edges": [{"source": dep, "target": key} for key, n in self.nodes.items() for dep in n["depends_on"]],
                "complete": not self.pending,
                "scope": "Dependencias y resultados observados; no garantiza corrección semántica ni exactitud de las fuentes."}

    def reopen_execution(self):
        """Una revisión concreta puede pedir corregir y volver a comprobar los archivos."""
        affected = set()
        for key, node in self.nodes.items():
            if node["tool"] in {"project_write", "sandbox_exec"} or affected.intersection(node["depends_on"]):
                affected.add(key)
                node["status"] = "pending"
                node.pop("evidence", None)
        return self.snapshot()
