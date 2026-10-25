"""Consulta REAL al buscador configurado; sin modelo ni API de pago."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import AgentError, Settings
from agent_core.tools import Tools


async def main(url):
    Settings.from_env()
    if url:
        os.environ["SEARXNG_URL"] = url
    try:
        sources = await Tools(None, 0, 12).search("Python documentation unittest")
    except AgentError as error:
        raise SystemExit(error.message) from None
    if not sources:
        raise SystemExit("El buscador respondió, pero no devolvió fuentes públicas. No se comprobó la investigación.")
    print(f"OK real: {len(sources)} resultados públicos con título, enlace y fragmento.")
    for source in sources:
        print(f"- {source['title']}: {source['url']}")
    print("No se leyeron páginas completas ni se entrenó un modelo.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="", help="SearXNG local, por ejemplo http://127.0.0.1:8888")
    asyncio.run(main(parser.parse_args().url))
