"""Comprueba ZYON con el modelo configurado y una conversación temporal."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import ConfigFiles, Settings
from agent_core.projects import ProjectFiles
from agent_core.service import AgentService
from agent_core.storage import Database


async def main():
    files = ConfigFiles(Settings.from_env().root)
    with tempfile.TemporaryDirectory(prefix='zyon-check-') as directory:
        database = Database(Path(directory) / 'check.sqlite3')
        database.initialize()
        user = database.authenticate('prueba_zyon', str(uuid.uuid4()), True, '127.0.0.1')
        conversation = database.create_conversation(user['id'], 'zyon', files.agent().default_backend)
        service = AgentService(files, database)
        start = await service.respond(user['id'], conversation['id'], str(uuid.uuid4()), 'zyon start')
        assert start['answer'] == '[Zyon]\n\nwhat are we operating on?' and start['model_calls'] == 0
        print('OK: activación ZYON sin inferencias ni herramientas.', flush=True)
        greeting = await service.respond(user['id'], conversation['id'], str(uuid.uuid4()), 'hola, ¿qué puedes hacer por mí? Responde brevemente.')
        assert greeting['answer'].startswith('[Zyon]\n') and greeting['answer'].count('[Zyon]') == 1, greeting
        assert greeting['model_calls'] > 0 and not greeting['tools_used'], greeting
        assert len(database.history(user['id'], conversation['id'])) == 4
        # Reproduce también la conversación que antes creaba archivos y agotaba pasos.
        project = ProjectFiles(database, user['id'], conversation['id'])
        project.write([{'path': 'anterior.py', 'content': 'print(4)\n'}])
        original = project.read()
        introduction = await service.respond(user['id'], conversation['id'], str(uuid.uuid4()), 'vamos a trabaja en unproyecto')
        assert introduction['answer'].startswith('[Zyon]\n'), introduction
        text = introduction['answer'].removeprefix('[Zyon]').strip().casefold()
        assert any(phrase in text for phrase in ('objetivo', 'qué quieres', 'qué necesitas', 'qué tipo')), introduction
        assert not text.startswith('pregunta por'), introduction
        assert introduction['tools_used'] == [] and introduction['task_graph'] is None, introduction
        assert introduction['model_calls'] <= 2 and introduction['review'] in {'model_reviewed', 'clarification'}, introduction
        assert project.read() == original, 'La aclaración modificó archivos existentes'
        assert len(database.history(user['id'], conversation['id'])) == 6
        print('OK: apertura de proyecto aclarada sin herramientas ni cambios de archivos.', flush=True)
        cybersecurity = await service.respond(user['id'], conversation['id'], str(uuid.uuid4()), 'quiero crea  proyectos de siberseguridad')
        assert cybersecurity['answer'].startswith('[Zyon]\n'), cybersecurity
        assert cybersecurity['tools_used'] == [] and cybersecurity['task_graph'] is None, cybersecurity
        assert cybersecurity['model_calls'] <= 2 and cybersecurity['review'] in {'model_reviewed', 'clarification'}, cybersecurity
        assert project.read() == original, 'La apertura de ciberseguridad modificó archivos existentes'
        assert len(database.history(user['id'], conversation['id'])) == 8
        print('OK: petición de ciberseguridad de la captura respondida sin agotar pasos ni ejecutar acciones.', flush=True)
        report = {'ok': True, 'backend': greeting['backend'], 'model': greeting['model'],
                  'model_calls': greeting['model_calls'], 'answer': greeting['answer'], 'review': greeting['review'],
                  'project_introduction': {'answer': introduction['answer'], 'model_calls': introduction['model_calls'],
                                           'tools_used': introduction['tools_used'], 'files_unchanged': True},
                  'cybersecurity_introduction': {'answer': cybersecurity['answer'], 'model_calls': cybersecurity['model_calls'],
                                                 'tools_used': cybersecurity['tools_used'], 'files_unchanged': True}}
        target = files.root / '.local' / 'zyon-checks' / 'live.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
