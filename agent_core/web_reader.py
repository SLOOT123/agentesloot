"""Acceso de red solo para el lector fijo; no habilita red a código del modelo."""
import json
from pathlib import Path

from .sandbox import SandboxRunner


class ReaderRunner(SandboxRunner):
    max_code_chars = 16000
    purpose = "web_reader"
    network = "bridge"

    def command(self, docker, name, language, project=False):
        command = super().command(docker, name, language, project)
        command[command.index("--network=none")] = "--network=bridge"
        return command


class WebReader:
    def __init__(self, config):
        self.runner = ReaderRunner(config)
        self.query = ""

    async def read(self, url):
        code = (Path(__file__).with_name("web_reader_worker.py")).read_text("utf-8")
        query = self.query[:1000] if isinstance(self.query, str) else ""
        result = await self.runner.run("python", code + "\nmain(" + repr(url) + ", " + repr(query) + ")\n")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error") or "reader_failed",
                    "message": result.get("message") or "El lector aislado no terminó correctamente."}
        try:
            payload = json.loads(result["stdout"])
            if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
                raise ValueError()
            return payload
        except (ValueError, KeyError):
            return {"ok": False, "error": "reader_invalid_output", "message": "El lector no devolvió un resultado válido."}
