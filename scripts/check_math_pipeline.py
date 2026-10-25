"""Ejecuta los modelos matemáticos reales del proyecto en Docker, sin modelo de IA ni red."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_core.config import ConfigFiles, Settings
from agent_core.sandbox import SandboxRunner


CODE = '''import json, math, time
from retrieval import tfidf_vectors, rank_sources, cosine
from retry_policy import backoff_delay
query = 'Python unittest pruebas'
sources = [
    {'url': 'https://example.org/cafe', 'title': 'Café', 'excerpt': 'Bebidas calientes'},
    {'url': 'https://docs.python.org/3/library/unittest.html', 'title': 'Python unittest', 'excerpt': 'Python pruebas unittest'}]
vector, vectors = tfidf_vectors(query, [s['title'] + ' ' + s['excerpt'] for s in sources])
assert math.isclose(sum(v*v for v in vector.values()), 1)
assert cosine(vector, vectors[0]) == 0
ranked = rank_sources(query, sources)
assert ranked[0]['url'] == sources[1]['url']
print(json.dumps({'phase': 'vectors', 'query_vector': vector, 'ranking': ranked}, ensure_ascii=False), flush=True)
time.sleep(1)
delays = [backoff_delay(failures) for failures in range(1, 7)]
assert delays == [300, 600, 1200, 2400, 3600, 3600]
assert backoff_delay(1, 900) == 900
print(json.dumps({'phase': 'retry_policy', 'calculated_delays_seconds': delays, 'retry_after_900_seconds': backoff_delay(1, 900)}), flush=True)
time.sleep(1)
print('MODEL_CHECKS_OK: vectores, ranking y espera comprobados; no se hicieron descargas ni peticiones HTTP.', flush=True)
'''


async def main():
    root = Settings.from_env().root
    runner = SandboxRunner(ConfigFiles(root).agent().sandbox)
    events = []
    def observe(kind, title, payload):
        event = {'kind': kind, 'title': title, **payload}
        if kind == 'sandbox_started':
            event['command'] = runner.command(shutil.which('docker') or 'docker', payload['container'], 'python', project=True)
        events.append(event)
        print(json.dumps(event, ensure_ascii=False), flush=True)
    runner.event = observe
    files = [{'path': name, 'content': (root / 'agent_core' / name).read_text('utf-8')}
             for name in ('retrieval.py', 'retry_policy.py')]
    result = await runner.run('python', CODE, files=files)
    report = {'ok': result.get('ok', False) and 'MODEL_CHECKS_OK' in result.get('stdout', ''),
              'result': result, 'events': events,
              'module_sha256': {item['path']: hashlib.sha256(item['content'].encode('utf-8')).hexdigest() for item in files},
              'scope': 'Código real del ranking y la política; casos controlados, sin descargar vídeo ni evaluar la IA.'}
    output = root / '.local' / 'math-checks' / 'live.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print('Informe: ' + str(output), flush=True)
    if not report['ok']:
        raise SystemExit('La comprobación en Docker no terminó correctamente: ' + result.get('message', result.get('error') or 'resultado inesperado'))


if __name__ == '__main__':
    asyncio.run(main())
