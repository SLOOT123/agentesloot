"""Descargas persistentes en segundo plano, independientes del tiempo del chat."""
import asyncio
import json
from pathlib import Path
import re
import shutil
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from .config import AgentError
from .downloads import Downloader, DownloadStore
from .download_worker import DownloadError, single_url
from .team_control import SkillRegistry
from .retry_policy import backoff_delay
from .tool_catalog import download_inventory, recovery_advice


def requested_download(message, history=()):
    prefix = r"^(?:(?:por favor|quiero que|quiero|puedes|puede|podrias|necesito|me|tu|agente)\s+)*(?:descarg\w*|baj(?:a|ar|ame)|extra(?:e|er))\b"
    urls = []
    for value in re.findall(r'https?://[^\s<>\[\]"\']+', message):
        value = value.rstrip(".,;!")
        while value.endswith(")") and value.count(")") > value.count("("):
            value = value[:-1]
        if value not in urls:
            urls.append(value)
    text = "".join(c for c in unicodedata.normalize("NFD", message.lower()) if not unicodedata.combining(c))
    text = text.lstrip(" \t\r\n¿¡")
    # El usuario puede pegar primero el enlace y escribir la orden después.
    command = re.sub(r'https?://[^\s<>\[\]"\']+', " ", text).strip(" \t\r\n[]():.,;!?¿¡-")
    intent = re.match(prefix, command)
    if not urls and intent and re.search(r'\b(?:ese|esa|este|esta|anterior|mismo|misma|enlace|pagina)\b', command):
        previous = next((row['content'] for row in reversed(history[-6:]) if row['role'] == 'user' and 'http' in row['content']), '')
        for value in re.findall(r'https?://[^\s<>\[\]"\']+', previous):
            value = value.rstrip('.,;!')
            while value.endswith(')') and value.count(')') > value.count('('): value = value[:-1]
            if value not in urls: urls.append(value)
    if len(urls) != 1:
        return None
    if not intent:
        # Un enlace como respuesta a una petición de descarga pendiente conserva esa intención.
        without_urls = re.sub(r'https?://[^\s<>\[\]"\']+', "", text).strip(" []():.")
        previous = next((row["content"] for row in reversed(history) if row["role"] == "user"), "")
        prior = "".join(c for c in unicodedata.normalize("NFD", previous.lower()) if not unicodedata.combining(c))
        if without_urls or not re.match(prefix, prior.strip()):
            return None
        text = prior
    # Los parámetros de la URL no describen lo que pidió el usuario.
    text = re.sub(r'https?://[^\s<>\[\]"\']+', "", text)
    audio_request = re.search(r"\baudio\s+(?:de|del)\b", text)
    video_request = re.search(r"\b(videos?|pelicula|videoclip)\b", text)
    extract_audio = (re.search(r"\b(?:solo\s+(?:el\s+)?audio|mp3|extra\w*\s+(?:el\s+)?audio)\b", text)
                     or audio_request and (not video_request or audio_request.start() < video_request.start()))
    if re.search(r'\bpdfs?\b', text):
        mode = 'pdf'
    elif extract_audio:
        mode = "audio"
    elif video_request:
        mode = "video"
    elif re.search(r"\b(audio|musica|cancion)\b", text):
        mode = "audio"
    elif re.search(r"\b(archivos?|documentos?|docx|xlsx|pptx)\b", text):
        mode = "file"
    else:
        mode = "audio" if re.search(r"(?:youtube\.com|youtu\.be)/", urls[0], re.I) else "file"
    return urls[0], mode


def download_followup(message):
    text = "".join(char for char in unicodedata.normalize("NFD", message.casefold()) if not unicodedata.combining(char))
    text = re.sub(r"\s+", " ", text).strip(" .!?¿¡")
    if len(text) > 220 or "http" in text or re.search(r"\b(analiz\w*|imagen\w*|fotos?|pdf|program\w*|codigo)\b", text):
        return None
    if (re.search(r"\b(avisa\w*|notifica\w*)\b", text) and re.search(r"\b(termin\w*|list[oa]s?|acabe\w*|descarg\w*)\b", text)
            and not re.search(r"\b(no|nunca)\b", text)):
        return "watch"
    if re.fullmatch(r"(?:por favor )?(?:(?:como|que tal) va(?: la descarga| el video| el audio)?|estado de (?:la|las) descargas?|ya (?:termino|terminaron|esta listo|esta lista|se descargo)(?: la descarga| el video| el audio)?|(?:la descarga|el video|el audio) (?:ya )?(?:termino|esta list[oa])|cuando (?:termina|estara list[oa]) (?:la descarga|el video|el audio))", text):
        return "status"
    return None


class DownloadJobs:
    SITE_INTERVAL = 3
    RATE_LIMIT_DELAY = 300
    MAX_AUTO_RETRIES = 2
    MAX_TRANSIENT_RETRIES = 2
    TRANSIENT_DELAY = 5
    WAIT_POLL_SECONDS = 30

    def __init__(self, database, files):
        self.database, self.files = database, files
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(2)
        self.closing = False
        self.site_locks = {}

    @staticmethod
    def site(url):
        host = urlsplit(url).hostname.lower()
        if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}:
            return 'youtube.com'
        return host.removeprefix('www.')

    def site_state(self, site):
        with self.database.connection() as connection:
            row = connection.execute('SELECT retry_at,failures FROM download_site_limits WHERE site=?', (site,)).fetchone()
        return dict(row) if row else {'retry_at': 0, 'failures': 0}

    def limit_site(self, site, result):
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT retry_at,failures FROM download_site_limits WHERE site=?', (site,)).fetchone()
            failures = row['failures'] + 1 if row else 1
            delay = backoff_delay(failures, result.get('retry_after_seconds'), base_seconds=self.RATE_LIMIT_DELAY)
            retry_at = max(row['retry_at'] if row else 0, time.time() + delay)
            connection.execute('INSERT INTO download_site_limits(site,retry_at,failures) VALUES(?,?,?) ON CONFLICT(site) DO UPDATE SET retry_at=excluded.retry_at,failures=excluded.failures', (site, retry_at, failures))
        return retry_at

    def release_site(self, site):
        with self.database.connection() as connection:
            connection.execute('INSERT INTO download_site_limits(site,retry_at,failures) VALUES(?,?,0) ON CONFLICT(site) DO UPDATE SET retry_at=MAX(download_site_limits.retry_at,excluded.retry_at),failures=0', (site, time.time() + self.SITE_INTERVAL))

    async def wait_for_site(self, identifier, site, progress):
        while True:
            state = self.site_state(site)
            recovery_at = progress.get('retry_at', 0) if progress.get('wait_reason') == 'temporary_error' else 0
            retry_at = max(state['retry_at'], recovery_at)
            remaining = retry_at - time.time()
            if remaining <= 0:
                return
            progress.update(phase='waiting', percent=None, retry_at=retry_at)
            message = ('El sitio limitó las peticiones (HTTP 429). Esperando para reintentar automáticamente.'
                       if state['failures'] else 'Esperando el turno de descarga de este sitio.')
            if recovery_at > state['retry_at']:
                message = f"Esperando para recuperar el fallo temporal. Intentos de recuperación: {progress.get('transient_retries', 0)}."
            if state['failures']:
                message += (f" Reintentos realizados: {progress.get('rate_limit_retries', 0)}; el objetivo sigue activo." if progress.get('retry_policy', {}).get('persistent_recovery')
                            else f" Reintentos automáticos usados: {progress.get('rate_limit_retries', 0)} de {self.MAX_AUTO_RETRIES}.")
            self._update(identifier, status='queued', message=message, progress_json=json.dumps(progress, ensure_ascii=False))
            await asyncio.sleep(min(remaining, self.WAIT_POLL_SECONDS))

    def source_available(self, identifier, identity, mode):
        directory = self.stage(identifier).resolve()
        try:
            manifest = directory / 'source.json'
            if manifest.stat().st_size > 16000:
                return False
            data = json.loads(manifest.read_text('utf-8'))
            if data['url'] != identity or data['mode'] != mode:
                return False
            for item in [data, *([data['audio']] if data.get('audio') else [])]:
                path = (directory / item['path']).resolve()
                if path.parent != directory or not path.is_file() or not 0 < path.stat().st_size == item['bytes']:
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _record(self, row):
        value = dict(row)
        value["progress"] = json.loads(value.pop("progress_json"))
        result = json.loads(value.pop("result_json"))
        value["download"] = result.get("download")
        value['steps'] = value['progress'].pop('steps', result.get('steps', []))
        value['source_available'] = self.source_available(value['id'], value['identity'], value['mode'])
        if value['status'] == 'failed' and value['error_code'] in {'site_access_required', 'protected_media'}:
            value['status'] = 'blocked'
        value['objective'] = value['progress'].get('objective', {
            'status': 'complete' if value['status'] == 'complete' else 'blocked' if value['status'] == 'blocked' else 'cancelled' if value['status'] == 'cancelled' else 'pending',
            'criterion': 'verified_file', 'purpose': value['mode']})
        value['objective']['status'] = {'complete': 'complete', 'blocked': 'blocked', 'cancelled': 'cancelled', 'queued': 'pending', 'running': 'active'}.get(value['status'], 'pending')
        if value['status'] == 'blocked' and not value['objective'].get('blocker'):
            value['objective']['blocker'] = recovery_advice(value['error_code'], value['message'], {'tools': []})
            value['message'] += ' Objetivo pendiente. ' + value['objective']['blocker']['next_action']
        value['progress']['objective'] = value['objective']
        if value['error_code'] == 'site_rate_limited' and value['status'] == 'failed' and 'quedó pendiente' in value['message']:
            value['message'] = 'El sitio limitó las peticiones (HTTP 429). La descarga se detuvo; puedes reintentar más tarde.'
        return value

    def list(self, user_id, conversation_id):
        self.database.conversation(user_id, conversation_id)
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM download_jobs WHERE conversation_id=? ORDER BY rowid DESC LIMIT 30", (conversation_id,)).fetchall()
        return [self._record(row) for row in rows]

    def get(self, user_id, conversation_id, job_id):
        self.database.conversation(user_id, conversation_id)
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM download_jobs WHERE id=? AND conversation_id=?", (job_id, conversation_id)).fetchone()
        if row is None:
            raise AgentError("download_job_missing", "La descarga no existe en esta conversación.", 404)
        return self._record(row)

    def _update(self, identifier, **fields):
        columns = {"status", "message", "error_code", "progress_json", "result_json"}
        if not fields or not set(fields) <= columns:
            raise ValueError("Campos de descarga no válidos")
        with self.database.connection() as connection:
            connection.execute("UPDATE download_jobs SET " + ",".join(key + "=?" for key in fields) + ",updated_at=CURRENT_TIMESTAMP WHERE id=?", [*fields.values(), identifier])

    def stage(self, identifier):
        return self.database.path.parent / ".local" / "downloads" / "staging" / str(uuid.UUID(identifier))

    def clean_stage(self, identifier):
        path = self.stage(identifier)
        root = self.database.path.parent / ".local" / "downloads" / "staging"
        if path.exists() and not path.is_symlink() and path.resolve().parent == root.resolve():
            try:
                shutil.rmtree(path)
            except OSError:
                pass  # Un archivo temporal aún abierto no invalida el archivo final ya comprobado.

    def prune_stage(self, identifier, identity, mode):
        """Acota temporales entre intentos; conserva las fuentes completas del manifiesto."""
        directory = self.stage(identifier)
        root = self.database.path.parent / '.local' / 'downloads' / 'staging'
        if not directory.exists() or directory.is_symlink() or directory.resolve().parent != root.resolve():
            return 0
        keep = set()
        if self.source_available(identifier, identity, mode):
            try:
                data = json.loads((directory / 'source.json').read_text('utf-8'))
                keep = {'source.json', data['path']}
                if data.get('audio'):
                    keep.add(data['audio']['path'])
            except (OSError, ValueError, KeyError, TypeError):
                return 0
        removed = 0
        for item in directory.iterdir():
            if item.name not in keep and item.is_file() and not item.is_symlink():
                try:
                    item.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def schedule(self, identifier):
        if identifier not in self.tasks and not self.closing:
            self.tasks[identifier] = asyncio.create_task(self._run(identifier))

    async def start(self):
        self.closing = False
        with self.database.connection() as connection:
            connection.execute("UPDATE download_jobs SET status='queued',message='Reanudando descarga tras reiniciar el servidor.' WHERE status='running'")
            identifiers = [row[0] for row in connection.execute("SELECT id FROM download_jobs WHERE status='queued'")]
        for identifier in identifiers:
            self.schedule(identifier)
        self.notify_pending()

    def watch(self, user_id, conversation_id, identifier, request_id):
        self.database.conversation(user_id, conversation_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM download_jobs WHERE id=? AND conversation_id=?", (identifier, conversation_id)).fetchone()
            if row is None: raise AgentError("download_job_missing", "La descarga no existe en esta conversación.", 404)
            watching = row["status"] in {"queued", "running"}
            if watching:
                connection.execute("INSERT INTO download_watches(job_id,conversation_id,request_id,enabled) VALUES(?,?,?,1) ON CONFLICT(job_id) DO UPDATE SET request_id=excluded.request_id,enabled=1", (identifier, conversation_id, request_id))
            return self._record(row), watching

    def notify_pending(self, identifier=None, conversation_id=None):
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("""SELECT j.*,w.request_id AS watch_request FROM download_jobs j JOIN download_watches w ON w.job_id=j.id
                WHERE w.enabled=1 AND j.status IN ('complete','failed','cancelled','blocked') AND (? IS NULL OR j.id=?) AND (? IS NULL OR j.conversation_id=?)""", (identifier, identifier, conversation_id, conversation_id)).fetchall()
            for row in rows:
                result = json.loads(row["result_json"])
                download = result.get("download") if row["status"] == "complete" else None
                if row["status"] == "complete" and download:
                    answer = "La descarga terminó: " + download["filename"] + ". El archivo comprobado ya está disponible para descargar."
                elif row["status"] == 'blocked': answer = 'El objetivo de descarga sigue pendiente: requiere una acción externa. ' + row['message']
                elif row["status"] == "cancelled": answer = "La descarga fue cancelada. No se preparó un archivo final."
                else: answer = "La descarga no se completó. " + row["message"]
                anchor = connection.execute("SELECT COALESCE(MAX(id),0) FROM agent_messages WHERE conversation_id=?", (row["conversation_id"],)).fetchone()[0]
                connection.execute("INSERT INTO download_notices(job_id,conversation_id,status,content,download_json,after_message_id) VALUES(?,?,?,?,?,?)", (row["id"], row["conversation_id"], row["status"], answer, json.dumps(download), anchor))
                connection.execute("UPDATE download_watches SET enabled=0 WHERE job_id=?", (row["id"],))

    def notices(self, user_id, conversation_id):
        self.database.conversation(user_id, conversation_id)
        self.notify_pending(conversation_id=conversation_id)
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM download_notices WHERE conversation_id=? ORDER BY id DESC LIMIT 30", (conversation_id,)).fetchall()
        return [{"id": "download-notice:" + str(row["id"]), "notification_id": row["id"], "job_id": row["job_id"],
                 "role": "assistant", "content": row["content"], "status": row["status"], "sources": [],
                 "download": json.loads(row["download_json"]), "after_message_id": row["after_message_id"], "created_at": row["created_at"]} for row in reversed(rows)]

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def submit(self, user_id, conversation_id, url, mode="audio", request_id=""):
        self.database.conversation(user_id, conversation_id)
        config = self.files.agent().downloads
        if not config.enabled:
            raise AgentError("downloads_disabled", "Las descargas están desactivadas en el servidor.", 422)
        try:
            identity = single_url(url)
        except (DownloadError, ValueError):
            raise AgentError("invalid_download_url", "Usa un enlace web público sin credenciales.", 422) from None
        if mode not in {"audio", "video", "file", "pdf"}:
            raise AgentError("invalid_download_mode", "El modo debe ser audio, video, file o pdf.", 422)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute("SELECT * FROM download_jobs WHERE conversation_id=? AND identity=? AND mode=?", (conversation_id, identity, mode)).fetchone()
            if existing and existing["status"] in {"queued", "running", "complete"}:
                job = self._record(existing)
                if job["status"] != "complete":
                    self.schedule(job["id"])
                return {"ok": True, "status": job["status"], "job": job, **({"download": job["download"]} if job["download"] else {})}
            count = connection.execute("SELECT COUNT(*) FROM download_jobs j JOIN agent_conversations c ON c.id=j.conversation_id WHERE c.user_id=? AND j.status IN ('queued','running','failed','blocked')", (user_id,)).fetchone()[0]
            if count >= 10 and not existing:
                raise AgentError("download_job_limit", "Hay demasiadas descargas pendientes. Cancela una antes de añadir otra.", 429)
            identifier = existing["id"] if existing else str(uuid.uuid4())
            if existing:
                progress = json.loads(existing['progress_json'])
                progress.pop('rate_limit_retries', None)
                progress.pop('transient_retries', None)
                progress.pop('retry_at', None)
                progress.pop('wait_reason', None)
                progress.pop('recovery', None)
                progress.update(phase='connecting', percent=None)
                connection.execute("UPDATE download_jobs SET status='queued',message='Preparando reintento de descarga.',error_code=NULL,result_json='{}',progress_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(progress, ensure_ascii=False), identifier))
            else:
                connection.execute("INSERT INTO download_jobs(id,conversation_id,request_id,url,identity,mode,status,message) VALUES(?,?,?,?,?,?,'queued','Descarga en cola.')", (identifier, conversation_id, request_id, url, identity, mode))
        self.schedule(identifier)
        return {"ok": True, "status": "queued", "job": self.get(user_id, conversation_id, identifier)}

    async def cancel(self, user_id, conversation_id, identifier):
        job = self.get(user_id, conversation_id, identifier)
        if job["status"] == "complete":
            raise AgentError("download_already_complete", "El archivo ya está disponible para descargar.", 409)
        task = self.tasks.get(identifier)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._update(identifier, status="cancelled", message="Descarga cancelada.")
        self.clean_stage(identifier)
        return self.get(user_id, conversation_id, identifier)

    async def _run(self, identifier):
        try:
            with self.database.connection() as connection:
                row = connection.execute("SELECT j.*,c.user_id FROM download_jobs j JOIN agent_conversations c ON c.id=j.conversation_id WHERE j.id=?", (identifier,)).fetchone()
            if row is None or row['status'] not in {'queued', 'running'}:
                return
            progress = json.loads(row['progress_json'])
            config = self.files.agent().downloads
            persistent = bool(getattr(config, 'persistent_recovery', False))
            progress['objective'] = {'status': 'active', 'criterion': 'verified_file', 'purpose': row['mode']}
            skill = SkillRegistry(getattr(self.files, 'root', Path(__file__).resolve().parent.parent)).load('download')
            progress['skill'] = {'id': skill['id'], 'version': skill['version'], 'acceptance_criteria': skill['acceptance_criteria']}
            progress['retry_policy'] = {'method': 'exponential_backoff', 'base_seconds': self.RATE_LIMIT_DELAY,
                'cap_seconds': 3600, 'max_auto_retries': self.MAX_AUTO_RETRIES, 'respects_retry_after': True,
                'temporary_error_base_seconds': self.TRANSIENT_DELAY, 'max_transient_retries': self.MAX_TRANSIENT_RETRIES}
            progress['retry_policy']['persistent_recovery'] = persistent
            # Un PID guardado antes de un reinicio no demuestra que el proceso siga vivo.
            if progress.get('process', {}).get('status') == 'running':
                progress['process']['status'] = 'unknown_after_restart'
            retained = progress.get('steps', [])
            if retained:
                progress['steps'] = retained[-30:]
            last = [0.0, '']
            def event(kind, title, item):
                if item.get('kind') != 'progress':
                    recorded = {key: item[key] for key in ('kind', 'method', 'operation', 'url', 'message', 'command', 'count', 'links', 'attempt', 'retry_at', 'automatic_retry', 'pid', 'runtime', 'status', 'exit_code', 'elapsed_seconds', 'delay_seconds', 'detail', 'level', 'extractor', 'tools', 'reason', 'next_action', 'documentation') if key in item}
                    recorded['created_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                    progress['steps'] = [*progress.get('steps', []), recorded][-40:]
                if item.get('kind') == 'process_started':
                    progress['process'] = {key: item[key] for key in ('pid', 'command', 'runtime', 'status') if key in item}
                elif item.get('kind') == 'process_finished':
                    progress.setdefault('process', {}).update({key: item[key] for key in ('pid', 'status', 'exit_code', 'elapsed_seconds') if key in item})
                if 'phase' in item and item['phase'] != progress.get('phase'):
                    progress['percent'] = None
                progress.update({key: item[key] for key in ('phase', 'percent', 'bytes', 'total_bytes', 'title', 'duration', 'speed', 'eta', 'seconds') if key in item})
                if item.get('kind') == 'attempt':
                    progress.update(phase='connecting', percent=None)
                phase = progress.get('phase', 'connecting')
                if item.get('kind') != 'progress' or time.monotonic() - last[0] >= 1 or last[1] != phase:
                    messages = {'connecting': 'Conectando con la fuente.', 'analyzing': 'Analizando la página y sus archivos disponibles.', 'downloading': 'Descargando archivo.', 'converting': 'Convirtiendo a MP3.', 'merging': 'Uniendo vídeo y sonido.', 'verifying': 'Comprobando el archivo.'}
                    self._update(identifier, progress_json=json.dumps(progress, ensure_ascii=False), message=messages.get(phase, title))
                    last[:] = [time.monotonic(), phase]
                if item.get('kind') != 'progress' and row['request_id']:
                    self.database.event(row['conversation_id'], row['request_id'], 'download', title, item)
            site = self.site(row['url'])
            lock = self.site_locks.setdefault(site, asyncio.Lock())
            if lock.locked():
                progress.update(phase='waiting', percent=None)
                self._update(identifier, status='queued', message='Esperando otra descarga de este sitio.', progress_json=json.dumps(progress, ensure_ascii=False))
            # La espera y la cola del sitio no consumen plazas de descarga globales.
            if retained:
                event('download', 'Trabajo de descarga retomado', {'kind': 'retry', 'method': 'worker', 'message': 'Ejecución retomada; se conservan los pasos anteriores.', 'phase': 'connecting'})
            store = DownloadStore(self.database, row['user_id'], row['conversation_id'])
            while True:
                async with lock:
                    await self.wait_for_site(identifier, site, progress)
                    async with self.semaphore:
                        removed = self.prune_stage(identifier, row['identity'], row['mode'])
                        if removed:
                            event('download', 'Temporales del intento anterior retirados', {'kind': 'stage_cleanup', 'method': 'store', 'count': removed,
                                'message': f'Temporales anteriores retirados: {removed}. Se conservan las fuentes completas registradas.'})
                        inventory = download_inventory()
                        if inventory != progress.get('tool_inventory'):
                            progress['tool_inventory'] = inventory
                            available = ', '.join(tool['name'] for tool in inventory['tools'] if tool['available']) or 'ninguna'
                            missing = ', '.join(tool['name'] for tool in inventory['tools'] if not tool['available'])
                            event('download', 'Descubrimiento de herramientas', {'kind': 'tool_discovery', 'method': 'catalog',
                                'message': 'Herramientas detectadas: ' + available + ('. No disponibles: ' + missing if missing else '.'),
                                'tools': inventory['tools']})
                        progress.pop('retry_at', None)
                        progress.pop('wait_reason', None)
                        progress.update(phase='connecting', percent=None)
                        self._update(identifier, status='running', error_code=None, message='Conectando con la fuente.', progress_json=json.dumps(progress, ensure_ascii=False))
                        result = await Downloader(store, self.files.agent().downloads, event).run(row['url'], row['mode'], workdir=self.stage(identifier))
                    if result.get('ok'):
                        self.release_site(site)
                        progress.pop('recovery', None)
                        progress['objective'].update(status='complete', evidence=result.get('download'))
                        event('download', 'Archivo comprobado', {'kind': 'verified', 'method': 'worker', 'message': 'Archivo recibido, comprobado y guardado en esta conversación.'})
                        self._update(identifier, status='complete', error_code=None, message='Archivo listo para descargar.', result_json=json.dumps(result, ensure_ascii=False),
                                     progress_json=json.dumps({**progress, 'phase': 'complete', 'percent': 100}))
                        self.clean_stage(identifier)
                        return
                    advice = recovery_advice(result.get('error'), result.get('message', ''), inventory)
                    progress['recovery'] = advice
                    event('download', 'Diagnóstico y siguiente acción', {'kind': 'recovery_plan', 'method': 'supervisor',
                        'message': advice['next_action'], **advice})
                    if result.get('error') in {'download_temporary', 'download_timeout'}:
                        retries = progress.get('transient_retries', 0)
                        if persistent or retries < self.MAX_TRANSIENT_RETRIES:
                            self.release_site(site)
                            delay = backoff_delay(retries + 1, result.get('retry_after_seconds'), base_seconds=self.TRANSIENT_DELAY, cap_seconds=60)
                            retry_at = time.time() + delay
                            progress.update(transient_retries=retries + 1, retry_at=retry_at, wait_reason='temporary_error', phase='waiting', percent=None)
                            message = (f'Objetivo pendiente. Recuperación automática {retries + 1} programada; se seguirá intentando hasta verificar el archivo o cancelar.' if persistent
                                       else f'Fallo temporal. Reintento de recuperación {retries + 1} de {self.MAX_TRANSIENT_RETRIES} programado; se conservan los archivos completos.')
                            event('download', 'Recuperación de descarga programada', {'kind': 'recovery_wait', 'method': 'worker', 'message': message, 'retry_at': retry_at, 'automatic_retry': retries + 1, 'delay_seconds': delay})
                            self._update(identifier, status='queued', error_code=result['error'], message=message, result_json=json.dumps(result, ensure_ascii=False), progress_json=json.dumps(progress, ensure_ascii=False))
                            continue
                        result = {**result, 'message': 'La descarga sigue fallando tras los reintentos de recuperación. ' + result.get('message', 'Puedes reintentar más tarde.')}
                    if result.get('error') == 'site_rate_limited':
                        retry_at = self.limit_site(site, result)
                        retries = progress.get('rate_limit_retries', 0)
                        if persistent or retries < self.MAX_AUTO_RETRIES:
                            progress.update(rate_limit_retries=retries + 1, retry_at=retry_at, wait_reason='site_rate_limit', phase='waiting', percent=None)
                            message = (f'El sitio limitó las peticiones (HTTP 429). Reintento automático {retries + 1} programado; el objetivo continúa pendiente.' if persistent
                                       else f'El sitio limitó las peticiones (HTTP 429). Reintento automático {retries + 1} de {self.MAX_AUTO_RETRIES} programado.')
                            event('download', 'Descarga en espera', {'kind': 'rate_limit_wait', 'method': 'worker', 'message': message, 'retry_at': retry_at, 'automatic_retry': retries + 1, 'delay_seconds': max(0, round(retry_at - time.time(), 3))})
                            self._update(identifier, status='queued', error_code='site_rate_limited', message=message, result_json=json.dumps(result, ensure_ascii=False), progress_json=json.dumps(progress, ensure_ascii=False))
                            continue
                        result = {**result, 'message': 'El sitio sigue limitando las peticiones (HTTP 429). Se agotaron los reintentos automáticos; puedes reintentar más tarde.'}
                    else:
                        self.release_site(site)
                    progress.pop('retry_at', None)
                    blocked = result.get('error') in {'site_access_required', 'protected_media'} or advice['reason'] == 'missing_dependency' or persistent and advice['status'] == 'blocked'
                    progress['objective'].update(status='blocked' if blocked else 'pending', blocker=advice if blocked else None)
                    progress.update(phase='blocked' if blocked else 'failed', percent=None)
                    message = result.get('message', 'No se pudo completar la descarga.')
                    if blocked:
                        message += ' Objetivo pendiente. ' + advice['next_action']
                    event('download', 'Objetivo bloqueado: requiere una acción' if blocked else 'Descarga detenida', {'kind': 'objective_blocked' if blocked else 'attempt_failed', 'method': 'supervisor', 'message': message})
                    self._update(identifier, status='blocked' if blocked else 'failed', error_code=result.get('error', 'download_failed'),
                                 message=message, result_json=json.dumps(result, ensure_ascii=False), progress_json=json.dumps(progress, ensure_ascii=False))
                    return
        except asyncio.CancelledError:
            self._update(identifier, status="queued" if self.closing else "cancelled", message="La descarga continuará al reiniciar." if self.closing else "Descarga cancelada.")
            raise
        except Exception as error:
            self._update(identifier, status="failed", error_code=getattr(error, "code", "download_failed"), message=getattr(error, "message", "No se pudo completar la descarga. Puedes reintentar."))
        finally:
            self.tasks.pop(identifier, None)
            if not self.closing:
                try: self.notify_pending(identifier)
                except Exception: pass  # La próxima consulta o reinicio vuelve a intentar el aviso persistente.
