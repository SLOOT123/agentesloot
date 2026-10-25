"""Monitor de terminal de los últimos estados registrados, con SQLite de solo lectura."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_core.config import Settings


def snapshot(path, conversation=None):
    # mode=ro impide crear o modificar la base, incluso si se escribe mal su ruta.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        downloads = []
        if 'download_jobs' in tables:
            rows = connection.execute('SELECT id,status,error_code,message,progress_json,updated_at FROM download_jobs WHERE (? IS NULL OR conversation_id=?) ORDER BY rowid DESC LIMIT 15', (conversation, conversation))
            for row in rows:
                progress = json.loads(row['progress_json'])
                retry_at = progress.get('retry_at')
                downloads.append({key: row[key] for key in ('id', 'status', 'error_code', 'message', 'updated_at')} | {
                    'phase': progress.get('phase'), 'percent': progress.get('percent'), 'retry_at': retry_at,
                    'remaining_seconds': max(0, round(retry_at - time.time())) if isinstance(retry_at, (int, float)) else None,
                    'automatic_retries_used': progress.get('rate_limit_retries', 0),
                    'automatic_recoveries_used': progress.get('transient_retries', 0),
                    'objective': progress.get('objective'), 'recovery': progress.get('recovery'),
                    'process': progress.get('process'), 'skill': progress.get('skill'), 'retry_policy': progress.get('retry_policy')})
                if row['status'] == 'failed' and row['error_code'] == 'site_rate_limited' and 'quedó pendiente' in row['message']:
                    downloads[-1]['message'] = 'El sitio limitó las peticiones (HTTP 429). La descarga se detuvo; puedes reintentar más tarde.'
        jobs = []
        if 'agent_jobs' in tables:
            jobs = [dict(row) for row in connection.execute('SELECT id,status,error_code,updated_at FROM agent_jobs WHERE (? IS NULL OR conversation_id=?) ORDER BY rowid DESC LIMIT 10', (conversation, conversation))]
        events = []
        if 'agent_events' in tables:
            for row in connection.execute("SELECT kind,title,payload,created_at FROM agent_events WHERE kind IN ('sandbox_started','sandbox_finished','retrieval') AND (? IS NULL OR conversation_id=?) ORDER BY id DESC LIMIT 6", (conversation, conversation)):
                payload = json.loads(row['payload'])
                fields = ('container', 'status', 'exit_code', 'network', 'method', 'candidates', 'returned')
                events.append({'kind': row['kind'], 'title': row['title'], 'created_at': row['created_at'],
                               **{key: payload[key] for key in fields if key in payload}})
    return {'observed_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'scope': 'Último estado registrado; un PID o contenedor histórico no demuestra que siga vivo.',
            'agent_jobs': jobs, 'downloads': downloads, 'events': events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--conversation')
    parser.add_argument('--watch', action='store_true', help='Consultar cambios hasta Ctrl+C o --duration.')
    parser.add_argument('--duration', type=float, default=60, help='Segundos de observación, predeterminado 60.')
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    if not .5 <= args.interval <= 60 or not 0 < args.duration <= 86400:
        parser.error('Usa interval entre 0.5 y 60 y duration entre 0 y 86400.')
    path = args.database or Settings.from_env().db_path
    deadline = time.monotonic() + args.duration
    try:
        while True:
            print(json.dumps(snapshot(path, args.conversation), ensure_ascii=False, indent=2), flush=True)
            if not args.watch or time.monotonic() >= deadline:
                break
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    except (OSError, sqlite3.Error, ValueError) as error:
        raise SystemExit('No se pudo leer el monitor: ' + str(error)) from None
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
