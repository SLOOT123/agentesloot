"""Equipo real y cola persistente con SQLite temporal, sin mensajes de prueba en la cuenta real."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import ConfigFiles, Settings
from agent_core.agent_jobs import AgentJobs
from agent_core.service import AgentService
from agent_core.storage import Database


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--profile", default="equipo")
    arguments = parser.parse_args()
    settings = Settings.from_env()
    files = ConfigFiles(settings.root)
    if not files.agent().multi_agent.enabled:
        raise SystemExit("El equipo no está activado en la configuración.")
    query = arguments.query or ("Crea promedio.py con una función promedio(valores) que devuelva el promedio y lance ValueError para una lista vacía. Guarda también test_promedio.py con unittest que compruebe [] y [2,4]. Ejecuta esos archivos en Docker y entrega el resultado de las pruebas."
        if arguments.code else "Busca en documentación oficial site:docs.python.org para qué sirven unittest.TestCase y assertEqual. Explica brevemente con fuentes leídas.")
    with tempfile.TemporaryDirectory(prefix="team-check-") as directory:
        database = Database(Path(directory) / "check.sqlite3")
        database.initialize()
        user = database.authenticate("prueba_equipo", str(uuid.uuid4()), True, "127.0.0.1")
        conversation = database.create_conversation(user["id"], arguments.profile, files.agent().default_backend)
        jobs = AgentJobs(database, files, AgentService(files, database))
        await jobs.start()
        started = time.monotonic()
        try:
            queued = await jobs.submit(user["id"], conversation["id"], str(uuid.uuid4()), query)
            print(json.dumps({"status": queued["status"], "queue_seconds": round(time.monotonic() - started, 3)}), flush=True)
            identifier = queued["job"]["request_id"]
            last, after = None, 0
            while True:
                job = jobs.get(user["id"], conversation["id"], identifier)
                if job["status"] != last:
                    print("Estado: " + job["status"], flush=True)
                    last = job["status"]
                activity = database.activity(user["id"], conversation["id"], identifier, after)
                for event in activity["events"]:
                    after = event["id"]
                    if event["kind"] in {"worker_started", "worker_completed", "team_plan", "team_review", "source_read", "team_completed", "error", "script", "team_repair"}:
                        payload = event["payload"]
                        print(json.dumps({"event": event["kind"], "title": event["title"], "worker": payload.get("worker_id"), "status": payload.get("status"), "url": payload.get("url")}, ensure_ascii=False), flush=True)
                if job["status"] not in {"queued", "running"}:
                    break
                if time.monotonic() - started > files.agent().multi_agent.timeout_seconds + 15:
                    raise RuntimeError("La cola no terminó dentro de su presupuesto.")
                await asyncio.sleep(1)
            result = job["result"]
            output = {"job_status": job["status"], "seconds": round(time.monotonic() - started, 2), "result": result,
                      "events": database.activity(user["id"], conversation["id"], identifier)["events"]}
            with database.connection() as connection:
                output["artifacts"] = [{"phase": row["phase"], "payload": json.loads(row["payload"])} for row in connection.execute("SELECT phase,payload FROM agent_team_checkpoints WHERE conversation_id=? AND request_id=?", (conversation["id"], identifier))]
            # Valida el identificador antes de usarlo como parte de una ruta.
            profile = files.profile(arguments.profile)
            target = settings.root / ".local" / "team-checks" / ((arguments.profile + "-" if arguments.profile != "equipo" else "") + ("coding.json" if arguments.code else "research.json"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(output, ensure_ascii=False, indent=2), "utf-8")
            print(json.dumps({"status": job["status"], "review": result.get("review"), "team_status": result.get("team", {}).get("status"),
                "seconds": output["seconds"], "model_calls": result.get("model_calls"), "tools": result.get("tools_used"), "sources": [item.get("url") for item in result.get("sources", [])], "answer": result.get("answer", job["message"])[:2200], "report": str(target)}, ensure_ascii=False), flush=True)
            if job["status"] != "complete" or result.get("team", {}).get("status") != "complete":
                recent = [{"kind": item["kind"], "payload": item["payload"]} for item in output["events"] if item["kind"] in {"observation", "task_graph", "error"}][-6:]
                print(json.dumps({"recent_events": recent}, ensure_ascii=False)[:7000], flush=True)
                raise SystemExit("La prueba observó un resultado incompleto; revisa el informe conservado.")
            if profile.presentation == "luis":
                from agent_core.presentation import LUIS_HEADER
                if not result["answer"].startswith(LUIS_HEADER):
                    raise SystemExit("La presentación de Luis no se aplicó a la respuesta final.")
            if profile.presentation == "breadcrumb":
                from agent_core.presentation import BREADCRUMB_HEADER
                if not result["answer"].startswith(BREADCRUMB_HEADER + "\n"):
                    raise SystemExit("La presentación de Breadcrumb no se aplicó a la respuesta final.")
            if arguments.code and (not result.get("tested_project") or result.get("project_unverified")):
                raise SystemExit("No se comprobó la versión final del proyecto.")
            if arguments.code and not any("PRUEBAS_COMPROBADAS:" in event["payload"].get("result", {}).get("stdout", "") and event["payload"]["result"].get("ok") for event in output["events"] if event["kind"] == "observation"):
                raise SystemExit("No se observó la ejecución de las pruebas guardadas.")
            if not arguments.code and (len(result.get("sources", [])) < 2 or any(item.get("retrieval") != "page_extract" for item in result["sources"])):
                raise SystemExit("No se verificaron al menos dos fuentes leídas.")
        finally:
            await jobs.close()


if __name__ == "__main__":
    asyncio.run(main())
