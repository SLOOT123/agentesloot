"""Herramientas fijas de descarga y archivos privados por conversación."""
import asyncio
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext

from .config import AgentError, DownloadConfig
from .download_worker import DownloadError, validate_url
from .sandbox import SandboxRunner


def download_tools():
    try:
        installed = version("yt-dlp")
    except PackageNotFoundError:
        installed = None
    return {"tools": [{"name": "http", "available": True, "purpose": "Archivos directos, PDF/documentos e incrustaciones de páginas."},
                      {"name": "yt-dlp", "available": bool(installed), "version": installed, "purpose": "Extrae audio/vídeo desde sitios compatibles."},
                      {"name": "ffmpeg", "available": bool(shutil.which("ffmpeg")), "purpose": "Convierte audio a MP3 y une vídeo y sonido."},
                      {"name": "ffprobe", "available": bool(shutil.which("ffprobe")), "purpose": "Comprueba pistas multimedia."},
                      {"name": "node", "available": bool(shutil.which("node")), "purpose": "Motor JavaScript para extractores compatibles."}],
            "strategy": "download_url selecciona métodos y reintenta; solo devuelve éxito con un archivo comprobado."}


class DownloadStore:
    def __init__(self, database, user_id, conversation_id):
        self.database, self.user_id, self.conversation_id = database, user_id, conversation_id
        self.root = database.path.parent / ".local" / "downloads"

    def list(self):
        self.database.conversation(self.user_id, self.conversation_id)
        with self.database.connection() as connection:
            rows = connection.execute("SELECT id,filename,media_type,bytes,sha256,source_url,created_at FROM agent_downloads WHERE conversation_id=? ORDER BY created_at,id", (self.conversation_id,)).fetchall()
        return [{**dict(row), "url": f"/api/agent/conversations/{self.conversation_id}/downloads/{row['id']}"} for row in rows]

    def path(self, download_id):
        return self.root / (str(uuid.UUID(str(download_id))) + ".bin")

    def get(self, download_id):
        record = next((item for item in self.list() if item["id"] == str(download_id)), None)
        if record is None or not self.path(download_id).is_file():
            raise AgentError("download_missing", "La descarga no existe en esta conversación.", 404)
        return record, self.path(download_id)

    def save(self, path, result, url):
        self.database.conversation(self.user_id, self.conversation_id)
        identifier = str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path(identifier)
        size = path.stat().st_size
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if size != result["bytes"] or digest != result["sha256"]:
            raise AgentError("download_integrity", "El archivo no coincide con la comprobación de descarga.", 422)
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                quota = connection.execute("SELECT COUNT(*),COALESCE(SUM(d.bytes),0) FROM agent_downloads d JOIN agent_conversations c ON c.id=d.conversation_id WHERE c.user_id=?", (self.user_id,)).fetchone()
                if quota[0] >= 100 or quota[1] + size > 1024 * 1024 * 1024:
                    raise AgentError("download_quota", "Se alcanzó el espacio de descargas de esta cuenta.", 422)
                path.replace(target)
                connection.execute("INSERT INTO agent_downloads(id,conversation_id,filename,media_type,bytes,sha256,source_url) VALUES(?,?,?,?,?,?,?)",
                                   (identifier, self.conversation_id, result["filename"], result["media_type"], size, digest, url))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return next(item for item in self.list() if item["id"] == identifier)


class Downloader:
    def __init__(self, store, config=None, event=None):
        self.store = store
        self.config = config or DownloadConfig()
        self.event = event or (lambda *args: None)

    async def stop(self, process):
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                       stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        if process.returncode is None:
            process.kill()
        await process.wait()

    async def run(self, url, mode="audio", *, workdir=None):
        if not self.config.enabled:
            return {"ok": False, "error": "downloads_disabled", "message": "Las descargas están desactivadas en el servidor."}
        try:
            validate_url(url)
        except (DownloadError, ValueError):
            raise AgentError("invalid_download_url", "Usa un enlace HTTP/HTTPS público, sin credenciales ni puertos especiales.", 422) from None
        if mode not in {"audio", "video", "file", "pdf"}:
            raise AgentError("invalid_download_mode", "El modo debe ser audio, video, file o pdf.", 422)
        self.store.root.mkdir(parents=True, exist_ok=True)
        if workdir is not None:
            workdir.mkdir(parents=True, exist_ok=True)
        directory = nullcontext(str(workdir)) if workdir is not None else tempfile.TemporaryDirectory(prefix="pending-", dir=self.store.root)
        with directory as temp:
            environment = SandboxRunner.environment()
            for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP"):
                environment[name] = temp
            command = [sys.executable, "-X", "utf8", "-I", "-B", str(Path(__file__).with_name("download_worker.py"))]
            process = await asyncio.create_subprocess_exec(*command,
                cwd=temp, env=environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name != "nt")
            started = time.monotonic()
            result = None
            attempts = []
            outcome = "failed"
            try:
                self.event("download", "Proceso de descarga iniciado", {"kind": "process_started", "method": "worker",
                    "pid": process.pid, "command": command, "runtime": "local_process", "status": "running",
                    "message": "Proceso local iniciado; las operaciones y su resultado se registran a continuación."})
                async with asyncio.timeout(self.config.timeout_seconds):
                    payload = {"url": url, "mode": mode, "max_bytes": self.config.max_file_mb * 1024 * 1024,
                               "timeout_seconds": self.config.timeout_seconds}
                    process.stdin.write(json.dumps(payload).encode("utf-8"))
                    await process.stdin.drain()
                    process.stdin.close()
                    while line := await process.stdout.readline():
                        if len(line) > 16000:
                            raise ValueError("Respuesta de descarga demasiado larga")
                        item = json.loads(line)
                        if item.get("kind") == "result":
                            result = item
                        else:
                            if item.get('kind') != 'progress':
                                attempts.append(item)
                                attempts = attempts[-40:]
                            labels = {'attempt': 'Probando herramienta', 'operation': 'Herramienta en ejecución', 'discovery': 'Enlaces de la página encontrados', 'attempt_failed': 'Intento de descarga fallido', 'metadata': 'Archivo identificado', 'console': 'Salida de herramienta', 'strategy': 'Estrategia de descarga'}
                            self.event("download", labels.get(item.get('kind'), 'Descarga') + ': ' + item.get("method", "comprobación"), item)
                    await process.wait()
                    outcome = "complete" if result and result.get("ok") and process.returncode == 0 else "failed"
            except TimeoutError:
                outcome = "timeout"
                result = {"ok": False, "error": "download_timeout", "message": "Se agotó el tiempo de esta descarga. Puedes reintentar; los archivos completos ya recibidos se conservan para continuar." if workdir is not None else "La descarga agotó su tiempo máximo; no se guardó un archivo incompleto.", "attempts": attempts[-6:]}
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except (ValueError, OSError, KeyError):
                result = {"ok": False, "error": "download_worker_failed", "message": "El descargador no devolvió un resultado válido."}
            finally:
                if process.returncode is None:
                    cleanup = asyncio.create_task(self.stop(process))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                self.event("download", "Proceso de descarga finalizado", {"kind": "process_finished", "method": "worker",
                    "pid": process.pid, "exit_code": process.returncode, "status": outcome,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "message": "El proceso terminó; el archivo solo queda disponible después de comprobar su integridad."})
            if result and result.get("ok") and process.returncode != 0:
                return {"ok": False, "error": "download_worker_failed", "message": "El proceso terminó con errores; no se registró un archivo final."}
            if not result or not result.get("ok"):
                return result or {"ok": False, "error": "download_failed", "message": "No se obtuvo un archivo completo."}
            path = (Path(temp) / result["path"]).resolve()
            if path.parent != Path(temp).resolve() or path.is_symlink() or not path.is_file():
                raise AgentError("invalid_download_file", "El descargador no produjo un archivo permitido.", 422)
            if not 0 < path.stat().st_size <= self.config.max_file_mb * 1024 * 1024:
                raise AgentError("download_limit", "El archivo está vacío o supera el tamaño permitido.", 422)
            record = self.store.save(path, result, url)
            return {"ok": True, "download": record, "steps": attempts[-40:], "validation": "Archivo completo, firma de formato, tamaño y SHA-256 comprobados; las pistas multimedia se verifican con ffprobe cuando está disponible."}
