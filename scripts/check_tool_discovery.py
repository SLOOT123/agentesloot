"""Descubrimiento exigido y respuesta del modelo local real; SQLite temporal, sin datos del usuario."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_core.config import AgentError, ConfigFiles, Settings
from agent_core.service import AgentService
from agent_core.storage import Database


async def main():
    root = Settings.from_env().root
    files = ConfigFiles(root)
    if files.agent().backends['local'].adapter != 'ollama':
        raise SystemExit('Esta prueba requiere el backend local Ollama; no realiza consultas de pago.')
    with tempfile.TemporaryDirectory(prefix='tool-discovery-') as directory:
        database = Database(Path(directory) / 'check.sqlite3')
        database.initialize()
        user = database.authenticate('prueba_herramientas', str(uuid.uuid4()), True, '127.0.0.1')
        conversation = database.create_conversation(user['id'], 'agente', 'local')['id']
        request = str(uuid.uuid4())
        error = None
        try:
            result = await AgentService(files, database).respond(user['id'], conversation, request,
                'Usa exclusivamente tool_discover con purpose="capacidades locales" y resume su resultado.')
        except AgentError as failure:
            error = {'code': failure.code, 'message': failure.message}
            result = {}
        activity = database.activity(user['id'], conversation, request)
        report = {'ok': 'tool_discover' in result.get('tools_used', []), 'result': result, 'error': error, 'activity': activity,
                  'scope': 'El servidor ejecuta el descubrimiento solicitado antes de permitir la respuesta del modelo local. SQLite temporal; no mide elección autónoma de la IA ni verifica descargas.'}
        output = root / '.local' / 'math-checks' / 'tool-discovery-live.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        print(json.dumps({'ok': report['ok'], 'tools_used': result.get('tools_used', []), 'report': str(output)}, ensure_ascii=False), flush=True)
        if not report['ok']:
            raise SystemExit('El agente no ejecutó tool_discover; el informe conserva lo que ocurrió.')


if __name__ == '__main__':
    asyncio.run(main())
