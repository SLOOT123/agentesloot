"""Prueba REAL del modelo y las herramientas; usa SQLite temporal, sin cuentas del proyecto."""
import argparse
import asyncio
import json
import tempfile
import time
import uuid
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import AgentError, ConfigFiles, Settings
from agent_core.projects import ProjectFiles
from agent_core.service import AgentService
from agent_core.storage import Database


async def main(backend, laboratory, research, general_web=False, agent_graph=False):
    settings = Settings.from_env()
    files = ConfigFiles(settings.root)
    backend = backend or files.agent().default_backend
    with tempfile.TemporaryDirectory(prefix="agent-live-") as temp:
        database = Database(Path(temp) / "prueba.sqlite3")
        database.initialize()
        user = database.authenticate("prueba_local", str(uuid.uuid4()), True, "127.0.0.1")
        service = AgentService(files, database)

        async def send(profile, message):
            conversation = database.create_conversation(user["id"], profile, backend)
            started = time.monotonic()
            print(f"Iniciando prueba REAL: {profile}", flush=True)
            try:
                result = await service.respond(user["id"], conversation["id"], str(uuid.uuid4()), message)
            except AgentError:
                events = database.activity(user["id"], conversation["id"])["events"]
                summary = [{"kind": e["kind"], "title": e["title"],
                            "result": e["payload"].get("result"), "code": e["payload"].get("code")} for e in events if e["kind"] in {"tool", "observation", "script", "error"}]
                print("Actividad de la prueba fallida: " + json.dumps(summary, ensure_ascii=False)[:8000], flush=True)
                raise
            print(f"Respuesta real en {time.monotonic() - started:.1f}s; herramientas: {result['tools_used']}", flush=True)
            return conversation["id"], result

        _, result = await send("general", "Responde únicamente: CONEXION_OK")
        if not result["answer"].strip():
            raise RuntimeError("El modelo no devolvió texto.")
        print("OK real: respuesta del modelo: " + result["answer"][:160], flush=True)

        marker = "Brisa" + str(int(uuid.uuid4().hex[:6], 16))
        database.add_document(user["id"], "Ficha de proyecto de pruebas", "El nombre exacto del proyecto de pruebas es " + marker)
        _, result = await send("general", "Según mi documento Ficha de proyecto de pruebas, ¿cuál es el nombre exacto de mi proyecto de pruebas? Devuelve ese nombre.")
        if marker not in result["answer"] or not any(s.get("document_id") for s in result["sources"]):
            print("Respuesta observada: " + result["answer"][:500], flush=True)
            print("Fuentes recuperadas: " + str([(s.get("kind"), s.get("title")) for s in result["sources"]]), flush=True)
            raise RuntimeError("No se verificó la recuperación del dato de memoria con el modelo real.")
        print("OK real: memoria privada recuperada y utilizada en la respuesta.", flush=True)

        if general_web:
            _, result = await send("general", "Usa web_search para buscar site:docs.python.org Python unittest documentación oficial. Explica en español para qué sirve unittest y cita las fuentes devueltas. No basta con responder de memoria.")
            if "web_search" not in result["tools_used"] or not any(s.get("kind") == "web" for s in result["sources"]):
                print("Respuesta observada: " + result["answer"][:700], flush=True)
                raise RuntimeError("Mi agente no utilizó la búsqueda web solicitada; no se considera una prueba pasada.")
            print("OK real: Mi agente solicitó búsqueda web y recibió fuentes de SearXNG.", flush=True)

        if laboratory:
            conversation, result = await send("laboratorio", "Crea y comprueba un proyecto mínimo. Primero llama project_write para guardar saludo.py cuyo contenido sea print('PRUEBA_REAL_OK'). Después usa sandbox_exec con project=true, language=python, code=import saludo y purpose=Comprobar saludo. Al importar saludo se ejecutará el archivo. Solo después informa del resultado. No basta con mostrar código en la respuesta.")
            project = ProjectFiles(database, user["id"], conversation).read()
            events = database.activity(user["id"], conversation)["events"]
            project_attempts = {e["payload"]["attempt_id"] for e in events if e["kind"] == "script" and e["payload"].get("project_files")}
            successful = any(e["kind"] == "observation" and e["payload"].get("name") == "sandbox_exec"
                             and e["payload"].get("result", {}).get("ok")
                             and e["payload"]["result"].get("attempt_id") in project_attempts
                             and "PRUEBA_REAL_OK" in e["payload"]["result"].get("stdout", "") for e in events)
            if not any(item["path"] == "saludo.py" for item in project) or not successful or result["review"] != "model_reviewed":
                print("Revisión observada: " + result["review"] + "; respuesta: " + result["answer"][:600], flush=True)
                print("Ejecuciones observadas: " + json.dumps([e["payload"] for e in events if e["kind"] in {"script", "observation"}], ensure_ascii=False)[:8000], flush=True)
                raise RuntimeError("El modelo no completó creación, ejecución real y revisión. Consulta sus capacidades; no se considera una prueba pasada.")
            print("OK real: el modelo guardó archivos, ejecutó el proyecto en Docker y revisó la respuesta.", flush=True)

        if research:
            _, result = await send("investigador", "site:docs.python.org Python unittest documentación oficial. Explica en español para qué sirve unittest y cita las fuentes consultadas.")
            if not any(s.get("kind") == "web" for s in result["sources"]):
                raise RuntimeError("La investigación no devolvió fuentes web.")
            print("OK real: investigación con fuentes de búsqueda y respuesta del modelo.", flush=True)
        if agent_graph:
            conversation, result = await send("agente", "Crea y ejecuta un proyecto mínimo. Planifica con task_plan dos pasos: guardar con project_write y probar con sandbox_exec dependiente de guardar. Guarda saludo.py con print('GRAFO_REAL_OK'). Ejecuta sandbox_exec con language=python, code=import saludo, purpose=Comprobar saludo, project=true. No añadas búsquedas ni otros archivos. Informa solo tras comprobar la salida.")
            events = database.activity(user["id"], conversation)["events"]
            successful = any(e["kind"] == "observation" and e["payload"].get("name") == "sandbox_exec"
                             and e["payload"].get("result", {}).get("ok")
                             and "GRAFO_REAL_OK" in e["payload"]["result"].get("stdout", "") for e in events)
            if (not successful or not result.get("task_graph", {}).get("complete")
                    or result.get("project_unverified") or result["review"] != "model_reviewed"):
                print("Actividad observada: " + json.dumps([e["payload"] for e in events if e["kind"] in {"observation", "script"}], ensure_ascii=False)[:8000], flush=True)
                print("Resultado observado: " + json.dumps(result, ensure_ascii=False)[:5000], flush=True)
                raise RuntimeError("El modelo no completó planificación, archivos, ejecución y revisión del grafo real.")
            print("OK real: plan sin ciclos, proyecto ejecutado en Docker, grafo completo y revisión final.", flush=True)
    print("Terminadas las pruebas solicitadas. No se guardaron cuentas ni mensajes de prueba en la base del proyecto.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pruebas reales de inferencia; una API externa puede consumir cuota. No entrena pesos.")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--laboratory", action="store_true", help="Comprueba creación y ejecución de proyecto con Docker real")
    parser.add_argument("--research", action="store_true", help="Comprueba investigación con SearXNG real")
    parser.add_argument("--general-web", action="store_true", help="Comprueba búsqueda solicitada por el modelo en Mi agente con SearXNG real")
    parser.add_argument("--agent-graph", action="store_true", help="Comprueba el perfil Agente con planificación, archivos y Docker reales")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.backend, args.laboratory, args.research, args.general_web, args.agent_graph))
    except AgentError as error:
        raise SystemExit(f"FALLO real: {error.code}: {error.message}") from None
    except RuntimeError as error:
        raise SystemExit(f"FALLO real: {error}") from None
