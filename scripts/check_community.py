"""Prueba el principal con Ollama real y SQLite temporal, sin publicar en el canal del usuario."""
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from agent_core.config import Settings


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="community-check-") as directory:
        application = create_app(Settings(root, Path(directory) / "check.sqlite3"))
        with TestClient(application) as client:
            password = str(uuid.uuid4())
            response = client.post("/auth", data={"username": "prueba_canal", "password": password, "password_confirm": password, "email": 'canal@example.invalid', "action": "register"}, follow_redirects=False)
            if response.status_code != 303:
                raise SystemExit("FALLO: no se pudo iniciar la sesión temporal")
            token = client.get("/api/session").json()["csrf_token"]
            started = time.monotonic()
            response = client.post("/api/messages", json={"message": "ola", "request_id": str(uuid.uuid4())}, headers={"X-CSRF-Token": token})
            response.raise_for_status()
            identifier = response.json()["message_id"]
            print(json.dumps({"message_id": identifier, "send_seconds": round(time.monotonic() - started, 3), "status": response.json()["assistant_status"]}), flush=True)
            deadline = time.monotonic() + 185
            previous = None
            while time.monotonic() < deadline:
                messages = client.get("/api/messages").json()
                original = next(item for item in messages if item["id"] == identifier)
                state = original["assistant_status"]
                if state != previous:
                    print("Estado: " + str(state), flush=True)
                    previous = state
                if state == "failed":
                    raise SystemExit("FALLO: " + original["assistant_error"])
                if state == "complete":
                    replies = [item for item in messages if item["reply_to"] == identifier and item["role"] == "assistant"]
                    if len(replies) != 1 or not replies[0]["message"].strip():
                        raise SystemExit("FALLO: la respuesta no quedó guardada una sola vez")
                    print(json.dumps({"ok": True, "answer": replies[0]["message"], "total_seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False), flush=True)
                    return
                time.sleep(.5)
            raise SystemExit("FALLO: tiempo máximo agotado")


if __name__ == "__main__":
    main()
