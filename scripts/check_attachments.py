"""Archivos y visión reales con cuenta/SQLite temporales; no publica en la cuenta del usuario."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

from fastapi.testclient import TestClient
import pymupdf
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import create_app
from agent_core.config import Settings


def main():
    root = Path(__file__).resolve().parent.parent
    image = Image.new("RGB", (960, 540), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 56)
    draw.text((50, 30), "FACTURA 452", font=font, fill="black")
    draw.rectangle((60, 150, 430, 450), fill="red")
    buffer = io.BytesIO(); image.save(buffer, "PNG"); picture = buffer.getvalue()
    pdf = pymupdf.open(); page = pdf.new_page(); page.insert_text((50, 70), "FACTURA PRIVADA: TOTAL 731 euros.", fontsize=18)
    text_pdf = pdf.tobytes(); pdf.close()
    pdf = pymupdf.open(); page = pdf.new_page(); page.insert_image(pymupdf.Rect(20, 50, 580, 365), stream=picture)
    scan_pdf = pdf.tobytes(); pdf.close()
    cases = [
        ("markdown", "plan.md", ("Notas preliminares sin relación.\n" * 2500 + "\nENTREGA: El código acordado es AZUL-7319.\n").encode(), "¿Cuál es el código de ENTREGA del archivo?", ["AZUL-7319"]),
        ("pdf", "factura.pdf", text_pdf, "¿Cuál es el total de la factura del PDF?", ["731"]),
        ("image", "foto.png", picture, "¿Qué número aparece y de qué color es el rectángulo de la imagen?", ["452", "rojo"]),
        ("scanned_pdf", "escaneado.pdf", scan_pdf, "¿Qué número aparece en la factura de la página 1 del PDF?", ["452"]),
    ]
    reports = []
    with tempfile.TemporaryDirectory(prefix="attachment-check-") as directory:
        with TestClient(create_app(Settings(root, Path(directory) / "check.sqlite3"))) as client:
            password = str(uuid.uuid4())
            response = client.post("/auth", data={"username": "prueba_archivos", "password": password, "password_confirm": password, "email": 'archivos@example.invalid', "action": "register"}, follow_redirects=False)
            if response.status_code != 303: raise SystemExit("No se pudo crear la sesión temporal.")
            headers = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
            for kind, name, content, query, expected in cases:
                conversation = client.post("/api/agent/conversations", json={"profile": "luis"}, headers=headers).json()["id"]
                start = time.monotonic()
                upload = client.post(f"/api/agent/conversations/{conversation}/attachments", headers=headers, files={"file": (name, content)})
                upload.raise_for_status()
                identifier = str(uuid.uuid4())
                submit = client.post(f"/api/agent/conversations/{conversation}/messages", headers=headers, json={"message": query, "request_id": identifier, "background": True})
                submit.raise_for_status()
                print("Prueba iniciada: " + kind, flush=True)
                while time.monotonic() - start < 610:
                    job = client.get(f"/api/agent/conversations/{conversation}/jobs/{identifier}").json()
                    if job["status"] not in {"queued", "running"}: break
                    time.sleep(.5)
                result = job.get("result", {})
                answer = result.get("answer", "")
                passed = job["status"] == "complete" and all(term.casefold() in answer.casefold() for term in expected)
                visual = any(source.get("retrieval") == "vision_model" for source in result.get("sources", []))
                passed = passed and bool(result.get("attachments_used")) and (visual if kind in {"image", "scanned_pdf"} else True)
                report = {"kind": kind, "ok": passed, "seconds": round(time.monotonic() - start, 2), "result": result, "job_status": job["status"], "message": job.get("message")}
                reports.append(report)
                print(json.dumps({"kind": kind, "ok": passed, "seconds": report["seconds"], "answer": answer[:2000], "message": job.get("message")}, ensure_ascii=False), flush=True)
                target = root / ".local" / "attachment-checks" / "live.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(reports, ensure_ascii=False, indent=2), "utf-8")
                if not passed: raise SystemExit("La comprobación real quedó incompleta; revisa .local/attachment-checks/live.json")


if __name__ == "__main__": main()
