"""Ejecuta exclusivamente en Docker; nunca hay fallback a Python/shell del host."""
import asyncio
import json
import logging
import os
import shutil
import uuid

from .config import SandboxConfig
from .projects import validate_files


PROJECT_BOOTSTRAP = '''import json, os, pathlib, subprocess, sys
payload = json.load(sys.stdin)
root = pathlib.Path('/tmp/project')
root.mkdir()
for item in payload['files']:
    path = root / item['path']
    if not path.resolve().is_relative_to(root) or path == root:
        raise ValueError('Ruta fuera del proyecto')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(item['content'], encoding='utf-8')
os.chdir(root)
if payload['language'] == 'python':
    sys.path.insert(0, str(root))
    sys.argv = ['<project-script>']
    exec(compile(payload['code'], '<project-script>', 'exec'), {'__name__': '__main__', '__file__': '<project-script>'})
else:
    raise SystemExit(subprocess.run(['/bin/sh', '-eu', '-c', payload['code']]).returncode)
'''


class OutputLimit(Exception):
    pass


class SandboxRunner:
    max_code_chars = 8000
    purpose = "code"
    network = "none"

    def __init__(self, config: SandboxConfig):
        self.config = config
        self.event = None

    def observe(self, kind, title, payload):
        if self.event:
            try:
                self.event(kind, title, payload)
            except Exception:
                logging.getLogger(__name__).warning("No se pudo registrar un evento del contenedor", exc_info=True)

    def command(self, docker: str, name: str, language: str, project=False):
        runtime = ["python", "-I", "-B", "-u", "-c", PROJECT_BOOTSTRAP] if project else (["python", "-I", "-B", "-u", "-"] if language == "python" else ["/bin/sh", "-eu", "-s"])
        c = self.config
        return [docker, "run", "--rm", "--pull=never", "--name", name, "--init",
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                f"--memory={c.memory_mb}m", f"--memory-swap={c.memory_mb}m", f"--cpus={c.cpu_limit}",
                f"--pids-limit={c.pids_limit}", "--user=65534:65534", "--ulimit=nofile=64:64",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=32m,mode=1777", "--workdir=/tmp",
                "--env=HOME=/tmp", "--env=LANG=C.UTF-8", "--entrypoint", runtime[0], "-i", c.image, *runtime[1:]]

    @staticmethod
    def environment():
        # El CLI necesita localizar Docker; no se heredan API keys del servidor.
        names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"}
        return {key: value for key, value in os.environ.items() if key.upper() in names}

    async def remove(self, docker, name):
        process = None
        try:
            async with asyncio.timeout(5):
                process = await asyncio.create_subprocess_exec(docker, "rm", "--force", name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=self.environment())
                await process.wait()
        except (TimeoutError, OSError):
            if process and process.returncode is None:
                process.kill()
                await process.wait()

    async def run(self, language: str, code: str, *, files=None):
        if not self.config.enabled:
            return {"ok": False, "error": "sandbox_disabled", "message": "La ejecución aislada está desactivada en config/agent.json."}
        if language not in {"python", "sh"} or not isinstance(code, str) or not code.strip() or len(code) > self.max_code_chars:
            return {"ok": False, "error": "invalid_script", "message": "Usa un script Python o sh de hasta " + str(self.max_code_chars) + " caracteres."}
        if files is not None:
            files = validate_files(files)
        docker = shutil.which("docker")
        if not docker:
            return {"ok": False, "error": "docker_unavailable", "message": "Docker no está instalado o no está en PATH. No se ejecutó código en el equipo."}
        name = "community-lab-" + uuid.uuid4().hex
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total = 0
        process = None
        reason = None
        updates, last_update = 0, 0
        metadata = {"container": name, "purpose": self.purpose, "language": language,
                    "network": self.network, "memory_mb": self.config.memory_mb,
                    "cpu_limit": self.config.cpu_limit, "timeout_seconds": self.config.timeout_seconds,
                    "command": self.command(docker, name, language, files is not None)}
        self.observe("sandbox_started", "Contenedor Docker solicitado", metadata)

        async def read(stream, key):
            nonlocal total, updates, last_update
            while chunk := await stream.read(2048):
                available = max(0, self.config.max_output_bytes - total)
                buffers[key].extend(chunk[:available])
                total += len(chunk)
                now = asyncio.get_running_loop().time()
                if updates < 24 and now - last_update >= .5:
                    self.observe("sandbox_output", "Salida recibida de Docker", {**metadata,
                        "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
                        "stderr": buffers["stderr"].decode("utf-8", errors="replace")})
                    updates += 1
                    last_update = now
                if total > self.config.max_output_bytes:
                    raise OutputLimit()

        async def write():
            try:
                payload = json.dumps({"language": language, "code": code, "files": files}, ensure_ascii=False) if files is not None else code
                process.stdin.write(payload.encode("utf-8") + b"\n")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        tasks = []
        try:
            process = await asyncio.create_subprocess_exec(*self.command(docker, name, language, files is not None),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.environment())
            tasks = [asyncio.create_task(write()), asyncio.create_task(read(process.stdout, "stdout")),
                     asyncio.create_task(read(process.stderr, "stderr")), asyncio.create_task(process.wait())]
            async with asyncio.timeout(self.config.timeout_seconds):
                await asyncio.gather(*tasks)
        except TimeoutError:
            reason = "script_timeout"
        except OutputLimit:
            reason = "output_limit"
        except OSError:
            reason = "docker_unavailable"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            if process and process.returncode is None:
                # Cancela el contenedor, no solo el cliente Docker, también al cancelar la petición HTTP.
                cleanup = asyncio.create_task(self.remove(docker, name))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                if process.returncode is None:
                    process.kill()
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if process:
                # En Windows las tuberías necesitan llegar a EOF incluso al cancelar una lectura.
                # El proceso ya terminó: se descartan los bytes restantes sin acumularlos.
                async def discard(stream):
                    while await stream.read(4096):
                        pass
                await asyncio.gather(discard(process.stdout), discard(process.stderr))
            exit_code = process.returncode if process else None
            self.observe("sandbox_finished", "Contenedor Docker finalizado", {**metadata,
                "status": "cancelled" if reason == "cancelled" else "complete" if exit_code == 0 and reason is None else "failed",
                "exit_code": exit_code, "error": reason or ("docker_unavailable" if exit_code == 125 else None),
                "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
                "stderr": buffers["stderr"].decode("utf-8", errors="replace")})
        exit_code = process.returncode if process else None
        if exit_code == 125 or reason == "docker_unavailable":
            return {"ok": False, "error": "docker_unavailable", "exit_code": exit_code,
                    "message": "Docker no pudo iniciar el contenedor. Comprueba Docker Desktop y la imagen configurada; no hay ejecución alternativa en el host."}
        return {"ok": exit_code == 0 and reason is None, "exit_code": exit_code, "error": reason,
                "stdout": buffers["stdout"].decode("utf-8", errors="replace"),
                "stderr": buffers["stderr"].decode("utf-8", errors="replace"),
                "validation": "Resultado de ejecución; la corrección del contenido necesita pruebas pertinentes."}
