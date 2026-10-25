"""Prepara búsqueda local, conservando las claves y direcciones ya configuradas."""
from pathlib import Path
import secrets

from dotenv import dotenv_values, set_key


def prepare(root):
    directory = root / ".local/search"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "search.env"
    if not path.exists():
        with path.open("x", encoding="utf-8") as stream:
            stream.write("SEARXNG_SECRET=" + secrets.token_hex(32) + "\n")
    application_env = root / ".env"
    if not application_env.exists():
        with application_env.open("x", encoding="utf-8") as stream:
            stream.write((root / ".env.example").read_text("utf-8"))
    if not (dotenv_values(application_env).get("SEARXNG_URL") or "").strip():
        set_key(str(application_env), "SEARXNG_URL", "http://127.0.0.1:8888")
    return path


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parent.parent)
    print("Configuración privada preparada en .local/search/search.env (se conserva si ya existe).")
    print("Iniciar: docker compose --env-file .local/search/search.env -f deploy/search/compose.yml up -d")
    print("SEARXNG_URL se completa con http://127.0.0.1:8888 solo si estaba vacío. Reinicia la aplicación.")
