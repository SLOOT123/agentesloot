"""Descubrimiento de capacidades registradas; no ejecuta ni instala programas de la web."""
import os
import shutil

from .downloads import download_tools
from .retrieval import cosine, tfidf_vectors


DOCUMENTATION = {
    'yt-dlp': 'https://github.com/yt-dlp/yt-dlp#readme',
    'ffmpeg': 'https://ffmpeg.org/documentation.html',
    'ffprobe': 'https://ffmpeg.org/ffprobe.html',
    'node': 'https://nodejs.org/en/download',
    'http': 'https://docs.python.org/3/library/urllib.request.html',
}


def download_inventory():
    report = download_tools()
    for item in report['tools']:
        item['documentation'] = DOCUMENTATION[item['name']]
    return report


def recovery_advice(error, message, inventory):
    installed = {item['name']: item['available'] for item in inventory['tools']}
    if error == 'site_access_required':
        return {'status': 'blocked', 'reason': 'authorized_access_required',
                'next_action': 'Habilitar acceso autorizado al contenido o proporcionar un enlace de descarga permitido. Repetir la misma petición no aporta autorización.',
                'documentation': 'https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp'}
    if error == 'protected_media':
        return {'status': 'blocked', 'reason': 'protected_media',
                'next_action': 'Usar una descarga ofrecida por el proveedor con permiso para obtener el archivo.'}
    lowered = str(message).casefold()
    missing = [name for name in ('yt-dlp', 'ffmpeg', 'ffprobe', 'node')
               if not installed.get(name) and name in lowered]
    if missing:
        return {'status': 'blocked', 'reason': 'missing_dependency', 'missing_tools': missing,
                'next_action': 'Habilitar las dependencias identificadas en el servidor y reanudar el mismo objetivo.',
                'documentation': DOCUMENTATION[missing[0]]}
    if error in {'site_rate_limited', 'download_temporary', 'download_timeout'}:
        return {'status': 'pending', 'reason': 'temporary_failure',
                'next_action': 'Esperar la pausa programada y comprobar las herramientas antes del siguiente intento.'}
    return {'status': 'blocked', 'reason': 'no_compatible_result',
            'next_action': 'Revisar el enlace individual, el formato pedido y los métodos observados; aportar una fuente compatible si el sitio no ofrece el archivo.'}


def discover(allowed, descriptions, purpose, *, sandbox_enabled=False, downloads_enabled=True):
    """Solo enumera las herramientas permitidas del turno; las puntuaciones no amplían permisos."""
    candidates = []
    for name in dict.fromkeys(allowed):
        if name == 'tool_discover' or name not in descriptions:
            continue
        available, readiness = True, 'local'
        if name in {'web_search', 'web_read'}:
            available = bool(os.environ.get('SEARXNG_URL')) if name == 'web_search' else sandbox_enabled and bool(shutil.which('docker'))
            readiness = 'configured_not_probed' if available else 'not_configured'
        elif name == 'sandbox_exec':
            available = sandbox_enabled and bool(shutil.which('docker'))
            readiness = 'installed_not_probed' if available else 'unavailable'
        elif name in {'download_url', 'download_tools'}:
            available = downloads_enabled
        candidates.append({'name': name, 'purpose': descriptions[name][:260], 'available': available, 'readiness': readiness})
    query, vectors = tfidf_vectors(purpose, [item['name'] + ' ' + item['purpose'] for item in candidates])
    for item, vector in zip(candidates, vectors):
        item['relevance'] = round(cosine(query, vector), 6)
    candidates.sort(key=lambda item: (not item['available'], -item['relevance']))
    return {'tools': candidates, 'download_tools': download_inventory() if any(name.startswith('download_') for name in allowed) else None,
            'scope': 'Capacidades registradas y permitidas; disponibilidad local o configuración, sin afirmar acceso remoto ni otorgar permisos nuevos.'}
