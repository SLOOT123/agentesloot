"""Prueba REAL: lector fijo en Docker, sin modelo, sin montar archivos del equipo."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import ConfigFiles, Settings
from agent_core.web_reader import WebReader


async def main():
    reader = WebReader(ConfigFiles(Settings.from_env().root).agent().sandbox)
    blocked = await reader.read("https://127.0.0.1/")
    assert not blocked["ok"], blocked
    assert blocked.get("error") == "private_destination", blocked
    reader.query = "unittest assertRaises expected exception context manager"
    result = await reader.read("https://docs.python.org/3/library/unittest.html")
    if not result.get("ok") or not result.get("text"):
        raise SystemExit("No se pudo comprobar una lectura pública real: " + str(result.get("error")) + " · " + result.get("message", "error"))
    assert len(result["text"]) <= 1800, result
    assert "assertRaises" in result["text"], result
    assert result.get("extraction") == "relevant_passages", result
    print("OK real: Docker rechazó un destino local y extrajo texto de documentación pública HTTPS.")
    print(f"Lectura acotada: {result['bytes_read']} bytes de respuesta, {len(result['text'])} caracteres de extracto; truncado={result['truncated']}.")
    print(result["scope"])
    print("Lector fijo, sin JavaScript, sin entrenamiento ni código de modelo con acceso a internet.")


if __name__ == "__main__":
    asyncio.run(main())
