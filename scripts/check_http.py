"""Prueba HTTP real sin crear cuentas, mensajes ni consultas al modelo."""
import argparse
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    with httpx.Client(base_url=arguments.url, timeout=10, trust_env=False) as client:
        for path, expected in [("/healthz", 200), ("/login", 200), ("/api/session", 401), ("/api/messages", 401),
                               ("/api/agent/profiles/zyon/behavior", 401),
                               ("/api/agent/config", 401), ("/api/agent/lessons", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/activity", 401), ("/.env", 404),
                               ("/static/agent.js", 200), ("/static/projects.js", 200), ("/static/activity.js", 200), ("/static/api.js", 200),
                               ("/static/attachments.js", 200), ("/static/monitor.js", 200),
                               ("/static/terminal.js", 200),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/attachments", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/attachments/00000000-0000-0000-0000-000000000000/content", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/attachments/00000000-0000-0000-0000-000000000000/preview", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/project", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/project.zip", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/jobs", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/jobs/00000000-0000-0000-0000-000000000000", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/team-artifacts/00000000-0000-0000-0000-000000000000", 401),
                               ("/api/agent/conversations/00000000-0000-0000-0000-000000000000/downloads/00000000-0000-0000-0000-000000000000", 401),
                               ("/static/effects.js", 200), ("/static/style.css", 200), ("/static/logo.jpg", 200)]:
            response = client.get(path)
            if response.status_code != expected:
                raise SystemExit(f"FALLO {path}: HTTP {response.status_code}, se esperaba {expected}")
            print(f"OK {path}: HTTP {response.status_code}")
        response = client.post("/api/agent/conversations/00000000-0000-0000-0000-000000000000/download-jobs", json={"url": "https://example.org/video", "mode": "video"})
        if response.status_code != 401:
            raise SystemExit(f"FALLO inicio de descarga: HTTP {response.status_code}")
        print("OK inicio de descarga: HTTP 401")
        base = "/api/agent/conversations/00000000-0000-0000-0000-000000000000/attachments"
        for response in (client.post(base, files={"file": ("prueba.txt", b"dato sintetico")}), client.delete(base + "/00000000-0000-0000-0000-000000000000")):
            if response.status_code != 401: raise SystemExit(f"FALLO modificación anónima de adjuntos: HTTP {response.status_code}")
        print("OK subida/eliminación de adjuntos: HTTP 401")
        response = client.post("/api/agent/conversations/00000000-0000-0000-0000-000000000000/jobs/00000000-0000-0000-0000-000000000000/cancel")
        if response.status_code != 401:
            raise SystemExit(f"FALLO cancelación de trabajo: HTTP {response.status_code}")
        print("OK cancelación de trabajo: HTTP 401")
        for action in ("retry", "cancel"):
            response = client.post(f"/api/agent/conversations/00000000-0000-0000-0000-000000000000/download-jobs/00000000-0000-0000-0000-000000000000/{action}")
            if response.status_code != 401:
                raise SystemExit(f"FALLO control de descarga {action}: HTTP {response.status_code}")
            print(f"OK control de descarga {action}: HTTP 401")
        response = client.get("/static/login.mp4", headers={"Range": "bytes=0-1023"})
        if response.status_code != 206 or len(response.content) != 1024:
            raise SystemExit("FALLO: el video no responde correctamente a una petición parcial")
        print("OK video: HTTP 206, intervalo de 1024 bytes")
        response = client.get("/api/agent/memory", headers={"Cookie": "session_user=cookie_heredada_falsa"})
        if response.status_code != 401:
            raise SystemExit("FALLO: una cookie heredada no debe autenticar al usuario")
        print("OK una cookie con un alias no concede acceso a memoria privada")


if __name__ == "__main__":
    main()
