"""Diagnóstico opcional; una API externa puede cobrar la consulta de prueba."""
import argparse
import asyncio
import json

from .config import AgentError, ConfigFiles, Settings
from .providers import Provider


async def check(backend_id):
    settings = Settings.from_env()
    config = ConfigFiles(settings.root).agent()
    backend_id = backend_id or config.default_backend
    if backend_id not in config.backends:
        raise AgentError("unknown_backend", "El proveedor no existe en config/agent.json.")
    async with asyncio.timeout(config.limits.run_timeout_seconds):
        return await Provider(config.backends[backend_id], config.limits).inspect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Comprueba el modelo configurado sin mostrar credenciales.")
    parser.add_argument("--backend", default=None)
    arguments = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(check(arguments.backend)), ensure_ascii=False, indent=2))
    except AgentError as error:
        print(f"{error.code}: {error.message}")
        raise SystemExit(1)
    except TimeoutError:
        print("provider_timeout: Se agotó el tiempo de comprobación.")
        raise SystemExit(1)
