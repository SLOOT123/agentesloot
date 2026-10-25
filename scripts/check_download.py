"""Descarga real con SQLite temporal; conserva únicamente el archivo pedido."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import ConfigFiles
from agent_core.downloads import Downloader, DownloadStore
from agent_core.service import AgentService
from agent_core.download_jobs import DownloadJobs
from agent_core.storage import Database


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--mode", choices=["audio", "video", "file", "pdf"], default="audio")
    parser.add_argument("--agent", action="store_true", help="Prueba también planificación y modelo local reales.")
    parser.add_argument("--background", action="store_true", help="Prueba la cola persistente y el progreso sin esperar en el chat.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    def event(kind, title, payload):
        print(title + ": " + json.dumps(payload, ensure_ascii=False)[:850], flush=True)
    with tempfile.TemporaryDirectory(prefix="download-check-") as temp:
        database = Database(Path(temp) / "check.sqlite3")
        database.initialize()
        user = database.authenticate("prueba_descarga", str(uuid.uuid4()), True, "127.0.0.1")
        conversation = database.create_conversation(user["id"], "agente", "local")
        store = DownloadStore(database, user["id"], conversation["id"])
        noun = {"audio": "audio en MP3", "video": "vídeo", "file": "archivo", "pdf": "PDF"}[args.mode]
        if args.background:
            jobs = DownloadJobs(database, ConfigFiles(root))
            await jobs.start()
            try:
                response = await AgentService(ConfigFiles(root), database, download_jobs=jobs).respond(
                    user["id"], conversation["id"], str(uuid.uuid4()), "Descarga el " + noun + " de este enlace: " + args.url)
                identifier = response["download_job"]["id"]
                print("Respuesta inmediata: " + response["answer"], flush=True)
                last = None
                while True:
                    job = jobs.get(user["id"], conversation["id"], identifier)
                    current = (job["status"], job["progress"].get("phase"), int(job["progress"].get("percent") or 0))
                    if current != last:
                        print(json.dumps({"status": job["status"], "message": job["message"], "progress": job["progress"]}, ensure_ascii=False), flush=True)
                        last = current
                    if job["status"] not in {"queued", "running"}:
                        result = {"ok": job["status"] == "complete", "job": job, **({"download": job["download"]} if job["download"] else {})}
                        break
                    await asyncio.sleep(2)
            finally:
                await jobs.close()
        elif args.agent:
            print("Probando el agente local con el enlace indicado…", flush=True)
            answer = await AgentService(ConfigFiles(root), database).respond(user["id"], conversation["id"], str(uuid.uuid4()),
                          "Descarga el " + noun + " de este enlace: " + args.url)
            items = store.list()
            result = {"ok": bool(items), "answer": answer["answer"], "tools_used": answer["tools_used"], "review": answer["review"], "download_count": len(items)}
            if items:
                result["download"] = items[-1]
        else:
            result = await Downloader(store, ConfigFiles(root).agent().downloads, event).run(args.url, args.mode)
        if result.get("ok"):
            item, path = store.get(result["download"]["id"])
            output = root / ".local" / "download-checks" / item["filename"]
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            result["output_path"] = str(output)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if not result.get("ok"):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
