"""Saludo real de Luis en una conversación temporal, sin publicar en la cuenta del usuario."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import ConfigFiles, Settings
from agent_core.presentation import LUIS_BANNER, LUIS_HEADER
from agent_core.service import AgentService
from agent_core.storage import Database


async def main():
    settings = Settings.from_env()
    files = ConfigFiles(settings.root)
    with tempfile.TemporaryDirectory(prefix="luis-check-") as directory:
        database = Database(Path(directory) / "check.sqlite3")
        database.initialize()
        user = database.authenticate("prueba_luis", str(uuid.uuid4()), True, "127.0.0.1")
        conversation = database.create_conversation(user["id"], "luis", files.agent().default_backend)
        result = await AgentService(files, database).respond(user["id"], conversation["id"], str(uuid.uuid4()), "ola")
        if not result["answer"].startswith(LUIS_HEADER) or LUIS_BANNER not in result["answer"] or result["tools_used"]:
            raise SystemExit("El saludo no conservó la presentación o ejecutó herramientas innecesarias.")
        if len(database.history(user["id"], conversation["id"], 10)) != 2:
            raise SystemExit("El saludo no quedó conservado una sola vez.")
        print(json.dumps({"ok": True, "model_calls": result["model_calls"], "answer": result["answer"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
