"""Descargador fijo: HTTP público y yt-dlp; nunca ejecuta código del modelo."""
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from email.message import Message
from email.utils import parsedate_to_datetime
from datetime import timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class DownloadError(Exception):
    pass


class ConversionError(DownloadError):
    pass


ORIGINAL_GETADDRINFO = socket.getaddrinfo
ORIGINAL_SOCKET = socket.socket


def public_ip(value):
    ip = ipaddress.ip_address(value)
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not (isinstance(ip, ipaddress.IPv6Address)
                     and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)))


def public_records(host, port, *args, **kwargs):
    if port not in {80, 443, "80", "443"}:
        raise DownloadError("Solo se admiten los puertos web 80 y 443.")
    records = ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)
    if not records or any(not public_ip(record[4][0]) for record in records):
        raise DownloadError("El enlace apunta a una dirección privada o no permitida.")
    return records


class PublicSocket(ORIGINAL_SOCKET):
    def connect(self, address):
        records = public_records(address[0], address[1], self.family, socket.SOCK_STREAM)
        return super().connect(records[0][4])

    def connect_ex(self, address):
        records = public_records(address[0], address[1], self.family, socket.SOCK_STREAM)
        return super().connect_ex(records[0][4])


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 for c in url):
        raise DownloadError("El enlace no es válido.")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}):
        raise DownloadError("Usa un enlace web público sin credenciales.")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise DownloadError("El enlace no es público.")
    try:
        if not public_ip(host):
            raise DownloadError("El enlace no es público.")
    except ValueError:
        pass
    return parsed


def single_url(url):
    parsed = validate_url(url)
    if parsed.hostname.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video = parse_qs(parsed.query).get("v")
        if video:
            return "https://www.youtube.com/watch?" + urlencode({"v": video[0]})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        validate_url(newurl)
        redirected = super().redirect_request(request, response, code, message, headers, newurl)
        if urlsplit(request.full_url).netloc != urlsplit(newurl).netloc:
            for name in ("Authorization", "Cookie"):
                redirected.remove_header(name)
        return redirected


class MediaLinks(HTMLParser):
    def __init__(self, base, mode="file"):
        super().__init__(convert_charrefs=True)
        self.base, self.mode, self.links = base, mode, []
        self.priority = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.mode == "video" and (tag == "audio" or str(attrs.get("type", "")).startswith("audio/")):
            return
        value = attrs.get("src") if tag in {"audio", "video", "source", "iframe", "embed"} else attrs.get("href") if tag == "a" else None
        if tag == "meta" and attrs.get("property", attrs.get("name", "")) in {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}:
            value = attrs.get("content")
        if not value:
            return
        url = urljoin(self.base, value)
        if tag == "a" and not (attrs.get("download") is not None or re.search(r"\.(mp3|m4a|wav|ogg|flac|opus|mp4|webm|mov|mkv|avi|m3u8|mpd)(?:[?#]|$)", url, re.I)):
            return
        if self.mode == "video" and re.search(r"\.(mp3|m4a|wav|ogg|flac|opus)(?:[?#]|$)", url, re.I):
            return
        try:
            validate_url(url)
        except (DownloadError, ValueError):
            return
        score = 2 if tag in {'audio', 'video', 'source'} or re.search(r'\.(mp3|m4a|wav|ogg|flac|opus|mp4|webm|mov|mkv|avi|m3u8|mpd)(?:[?#]|$)', url, re.I) else 1
        if url not in self.links and (len(self.links) < 6 or score > self.priority[self.links[-1]]):
            self.links.append(url); self.priority[url] = score
            self.links.sort(key=lambda link: -self.priority[link])
            if len(self.links) > 6:
                self.priority.pop(self.links.pop())


class DocumentLinks(HTMLParser):
    """Enlaces ofrecidos por la página, sin ejecutar scripts ni recorrer el sitio."""
    def __init__(self, base, pdf_only=False):
        super().__init__(convert_charrefs=True)
        self.base, self.pdf_only, self.candidates = base, pdf_only, {}
        self.anchor = None

    @property
    def links(self):
        return sorted(self.candidates, key=lambda url: -self.candidates[url])[:12]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.anchor = [attrs.get('href', ''), '']
        value = attrs.get('href') if tag in {'a', 'link'} else attrs.get('data') if tag == 'object' else attrs.get('src') if tag in {'iframe', 'embed'} else None
        citation = tag == 'meta' and attrs.get('name', attrs.get('property', '')).lower() in {'citation_pdf_url', 'dc.identifier', 'eprints.document_url'}
        if citation:
            value = attrs.get('content')
        if not value:
            return
        url = urljoin(self.base, value)
        mime = attrs.get('type', '').lower().split(';')[0]
        pdf = bool(re.search(r'\.pdf(?:[?#]|$)', url, re.I) or mime == 'application/pdf' or citation and attrs.get('name', '').lower() == 'citation_pdf_url')
        document = bool(re.search(r'\.(docx|xlsx|pptx|odt|ods|odp|zip)(?:[?#]|$)', url, re.I) or mime.startswith(('application/vnd.', 'application/msword')))
        offered = attrs.get('download') is not None and (not self.pdf_only or re.search(r'\.pdf$', attrs.get('download', ''), re.I))
        if not (pdf or (not self.pdf_only and document) or offered):
            return
        try:
            validate_url(url)
        except (DownloadError, ValueError):
            return
        if len(self.candidates) < 30 or url in self.candidates:
            self.candidates[url] = max(self.candidates.get(url, 0), 3 if pdf else 2 if document else 1)

    def handle_data(self, data):
        if self.anchor:
            self.anchor[1] = (self.anchor[1] + data)[:180]

    def handle_endtag(self, tag):
        if tag != 'a' or not self.anchor:
            return
        value, label = self.anchor
        self.anchor = None
        pdf = bool(re.search(r'\bpdf\b', label, re.I))
        document = bool(re.search(r'(?:download|descarg\w*)\s+(?:el\s+)?(?:documento|document|paper|docx|xlsx|pptx)\b', label, re.I))
        if value and (pdf or document and not self.pdf_only):
            url = urljoin(self.base, value)
            try:
                validate_url(url)
            except (DownloadError, ValueError):
                return
            if len(self.candidates) < 30 or url in self.candidates:
                self.candidates[url] = max(self.candidates.get(url, 0), 2 if pdf else 1)


def small_zip_index(path):
    # Acota el índice antes de que ZipFile cargue sus entradas. No extrae archivos.
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65557))
        tail = stream.read(65557)
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or len(tail) - position < 22:
        return False
    entries = struct.unpack_from('<H', tail, position + 10)[0]
    index_bytes = struct.unpack_from('<I', tail, position + 12)[0]
    return entries <= 5000 and index_bytes <= 2_000_000


def signature(path):
    with path.open("rb") as stream:
        data = stream.read(512)
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224):
        return ".mp3", "audio/mpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return ".avi", "video/x-msvideo"
    if len(data) > 376 and all(data[offset] == 0x47 for offset in (0, 188, 376)):
        return ".ts", "video/mp2t"
    if data.startswith(b"OggS"):
        return ".ogg", "audio/ogg"
    if data.startswith(b"fLaC"):
        return ".flac", "audio/flac"
    if data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            return ".mov", "video/quicktime"
        return (".m4a", "audio/mp4") if data[8:12] in {b"M4A ", b"M4B "} else (".mp4", "video/mp4")
    if data.startswith(b"\x1aE\xdf\xa3"):
        return ".webm", "video/webm"
    if data.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            if not small_zip_index(path):
                return '.zip', 'application/zip'
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if '[Content_Types].xml' in names:
                    for marker, extension, kind in [('word/document.xml', '.docx', 'wordprocessingml.document'), ('xl/workbook.xml', '.xlsx', 'spreadsheetml.sheet'), ('ppt/presentation.xml', '.pptx', 'presentationml.presentation')]:
                        if marker in names:
                            return extension, 'application/vnd.openxmlformats-officedocument.' + kind
                if 'mimetype' in names and archive.getinfo('mimetype').file_size < 150:
                    mime = archive.read('mimetype').decode('ascii', errors='replace')
                    formats = {'application/vnd.oasis.opendocument.text': '.odt', 'application/vnd.oasis.opendocument.spreadsheet': '.ods', 'application/vnd.oasis.opendocument.presentation': '.odp'}
                    if mime in formats:
                        return formats[mime], mime
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            pass
        return ".zip", "application/zip"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    raise DownloadError("La respuesta no es un archivo compatible; podría ser una página HTML o un bloqueo.")


def direct(url, target, limit, mode="file"):
    opener = build_opener(ProxyHandler({}), PublicRedirect())
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 FashTochDownloader/1.0", "Accept-Encoding": "identity"})
    with opener.open(request, timeout=12) as response:
        validate_url(response.url)
        if response.status != 200 or response.headers.get("Content-Range"):
            raise DownloadError("El servidor no entregó un archivo completo (HTTP 200).")
        content_type = response.headers.get("Content-Type", "").lower()
        expected = int(response.headers.get('Content-Length', '0'))
        if expected > limit and not any(kind in content_type for kind in ('text/html', 'application/xhtml+xml')):
            raise DownloadError('El archivo supera el tamaño permitido.')
        first = response.read(1024)
        if 'text/html' in content_type or 'application/xhtml+xml' in content_type or re.match(br'\s*(?:\xef\xbb\xbf)?\s*(?:<!doctype\s+html|<html\b)', first, re.I):
            page = (first + response.read(512000 - len(first))).decode('utf-8', errors='replace')
            media, documents = MediaLinks(response.url, mode), DocumentLinks(response.url, mode == 'pdf')
            media.feed(page); documents.feed(page)
            links = documents.links if mode == 'pdf' else list(dict.fromkeys(documents.links + media.links)) if mode == 'file' else media.links
            if not links and re.search(r'(?:sign in|log in|subscribe|purchase|inici[ae] sesi[oó]n|suscr[ií]b|compra).{0,100}(?:download|descarg|access|acced)', re.sub('<[^>]*>', ' ', page), re.I):
                raise DownloadError('El sitio exige login o suscripción para descargar.')
            return {"links": links}
        if expected > limit:
            raise DownloadError("El archivo supera el tamaño permitido.")
        size = 0
        last_progress = 0
        with target.open("wb") as stream:
            chunk = first
            while chunk:
                size += len(chunk)
                if size > limit:
                    raise DownloadError("El archivo supera el tamaño permitido.")
                stream.write(chunk)
                if time.monotonic() - last_progress >= 1:
                    emit("progress", phase="downloading", bytes=size, total_bytes=expected,
                         percent=min(100, round(size * 100 / expected, 1)) if expected else None)
                    last_progress = time.monotonic()
                chunk = response.read(65536)
        if size == 0 or expected and size != expected:
            raise DownloadError("La descarga está vacía o incompleta.")
        emit('progress', phase='downloading', bytes=size, total_bytes=expected or size, percent=100)
        signature(target)
        header = Message(); header['Content-Disposition'] = response.headers.get('Content-Disposition', '')
        filename = header.get_filename()
        return {"path": target, "title": filename if isinstance(filename, str) and filename.strip() else Path(urlsplit(response.url).path).name or "descarga"}


def emit(kind, **payload):
    print(json.dumps({"kind": kind, **payload}, ensure_ascii=False), flush=True)


def failure_message(error):
    detail = re.sub(r"\x1b\[[0-9;]*m", "", str(error))[-700:]
    lowered = detail.lower()
    if getattr(error, 'code', None) == 429 or "429" in lowered or "too many requests" in lowered:
        return "site_rate_limited", "El sitio limitó las peticiones (HTTP 429). No se completó la descarga.", detail
    if getattr(error, 'code', None) in {401, 402, 403} or any(word in lowered for word in ("sign in", "not a bot", "login", "private video", "authentication", "http error 401", "http error 402", "http error 403", 'payment required', 'subscription required', 'purchase required')):
        return "site_access_required", "El sitio exige autorización, inicio de sesión o confirmación de acceso. Usa un enlace de descarga habilitado con tu acceso autorizado; no se obtuvo el archivo.", detail
    if "unsupported url" in lowered:
        return "unsupported_download_url", "El enlace no identifica un archivo o vídeo compatible. Pega el enlace del contenido individual, no la página principal del sitio.", detail
    if "drm" in lowered:
        return "protected_media", "El contenido tiene protección DRM y no se obtuvo un archivo descargable.", detail
    if getattr(error, 'code', None) in {408, 500, 502, 503, 504} or isinstance(error, (TimeoutError, ConnectionError)) or any(word in lowered for word in ('timed out', 'timeout', 'connection reset', 'connection aborted', 'temporary failure', 'temporarily unavailable', 'remote end closed')):
        return "download_temporary", "La conexión o el servidor fallaron temporalmente. Se puede reintentar conservando los archivos completos.", detail
    return "download_failed", detail, detail


def matching_extractor(url, mode):
    """Clasifica la URL con las reglas del yt-dlp instalado, sin consultar el sitio."""
    if mode not in {'audio', 'video'} or importlib.util.find_spec('yt_dlp') is None:
        return None
    from yt_dlp.extractor import gen_extractor_classes
    for extractor in gen_extractor_classes():
        if extractor.IE_NAME != 'generic' and extractor.suitable(url):
            return extractor.IE_NAME
    return None


class DownloadLogger:
    """Salida real y acotada de yt-dlp dentro del protocolo JSON del trabajador."""
    def __init__(self):
        self.count = 0

    def send(self, level, message):
        if self.count >= 24:
            return
        value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(message))
        value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value).strip()
        if not value or value.startswith('[debug]'):
            return
        self.count += 1
        emit('console', method='yt-dlp', level=level, message=value[:2000])

    def debug(self, message):
        self.send('info', message)

    def info(self, message):
        self.send('info', message)

    def warning(self, message):
        self.send('warning', message)

    def error(self, message):
        self.send('error', message)


def retry_after_seconds(error):
    """Conserva Retry-After incluso cuando yt-dlp envuelve el error HTTP."""
    pending, seen = [error], set()
    for _ in range(12):
        if not pending:
            break
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        headers = getattr(current, 'headers', None)
        if headers is None:
            headers = getattr(getattr(current, 'response', None), 'headers', None)
        value = headers.get('Retry-After') if headers is not None else None
        if isinstance(value, str) and len(value) <= 128:
            value = value.strip()
            try:
                if value.isascii() and value.isdecimal():
                    return int(value) if len(value) <= 10 else None
                date = parsedate_to_datetime(value)
                return max(0, math.ceil(date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - time.time()))
            except (ValueError, TypeError, OverflowError):
                pass
        pending.extend(getattr(current, name, None) for name in ('__cause__', '__context__', 'cause'))
        info = getattr(current, 'exc_info', None)
        if isinstance(info, tuple) and len(info) > 1:
            pending.append(info[1])
    return None


def select_media_formats(ydl, formats, mode, limit):
    if mode in {"audio", "video"} and all(item.get("acodec" if mode == "audio" else "vcodec") == "none" for item in formats):
        raise DownloadError("El enlace no contiene la pista solicitada: " + mode + ".")
    if mode != "video":
        expression = "bestaudio/best" if mode == "audio" else "best/bestvideo"
        selected = ydl._select_formats(formats, ydl.build_format_selector(expression))
        return selected[0].get("requested_formats", [selected[0]]) if selected else []
    has_audio = any(item.get("acodec") != "none" for item in formats)
    # Se prefiere MP4/H.264 con audio M4A; la unión se realiza después, solo con archivos locales.
    for height in (1080, 720, 480, 360):
        expression = (f"bv[height<=?{height}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
                      f"b[height<=?{height}][ext=mp4]/bv[height<=?{height}]+ba/b[height<=?{height}]")
        if not has_audio:
            expression = f"bv[height<=?{height}]/b[height<=?{height}]"
        selected = ydl._select_formats(formats, ydl.build_format_selector(expression))
        if selected:
            tracks = selected[0].get("requested_formats", [selected[0]])
            sizes = [item.get("filesize") or item.get("filesize_approx") for item in tracks]
            if not all(sizes) or sum(sizes) <= limit:
                return tracks
    raise DownloadError("No hay un vídeo compatible dentro del tamaño permitido.")


def reject_external_network_download(*args, **kwargs):
    raise DownloadError("Este formato requiere un transporte externo no compatible; no se obtuvo el vídeo.")


def media_download(url, directory, mode, limit, deadline):
    if importlib.util.find_spec("yt_dlp") is None:
        raise DownloadError("Falta yt-dlp. Instala las dependencias de descarga del proyecto.")
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.external import FFmpegFD
    from yt_dlp.networking._urllib import UrllibRH
    # HLS nativo puede intentar delegar formatos no compatibles a FFmpeg.
    # Este proceso solo permite red a través de PublicSocket y FFmpeg con entradas locales.
    FFmpegFD.real_download = reject_external_network_download

    last_progress, completed, track_index, estimates = 0, 0, 0, []
    def progress(data):
        nonlocal last_progress
        if time.monotonic() > deadline:
            raise DownloadError("La descarga agotó el tiempo disponible.")
        downloaded = completed + (data.get("downloaded_bytes") or 0)
        if max(downloaded, completed + (data.get("total_bytes") or 0)) > limit:
            raise DownloadError("El archivo supera el tamaño permitido.")
        now = time.monotonic()
        if now - last_progress >= 1 or data.get("status") == "finished":
            estimates[track_index] = data.get("total_bytes") or data.get("total_bytes_estimate") or estimates[track_index]
            total = sum(estimates) if all(estimates) else 0
            emit("progress", phase="downloading", bytes=downloaded, total_bytes=total,
                 percent=min(100, round(downloaded * 100 / total, 1)) if total else None,
                 speed=data.get('speed'), eta=data.get('eta'))
            last_progress = now

    def media_filter(info, *, incomplete=False):
        if info.get("is_live") or info.get("has_drm"):
            return "No se descargan emisiones en vivo ni contenido con DRM."

    prefix = "media-" + os.urandom(6).hex()
    logger = DownloadLogger()
    options = {"quiet": True, "no_warnings": True, "logger": logger, "noplaylist": True,
               "playlistend": 1, "cachedir": False, "proxy": "", "socket_timeout": 10,
               "retries": 2, "fragment_retries": 2, "extractor_retries": 1, "concurrent_fragment_downloads": 1,
               "max_filesize": limit, "outtmpl": str(directory / (prefix + ".%(ext)s")), "restrictfilenames": True,
               "windowsfilenames": True, "overwrites": True, "progress_hooks": [progress],
               "match_filter": media_filter, "external_downloader": "native", "hls_prefer_native": True,
               "fixup": "never", "format": "bestaudio/best" if mode == "audio" else "best/bestvideo",
               "skip_unavailable_fragments": False,
               "js_runtimes": {"node": {"path": shutil.which("node")}} if shutil.which("node") else {},
               "remote_components": set(), "usenetrc": False, "cookiefile": None, "color": {"stderr": "never", "stdout": "never"}}
    with YoutubeDL(options) as ydl:
        # Una sola implementación Python: todas sus conexiones pasan por PublicSocket.
        ydl._request_director.handlers = {"Urllib": UrllibRH(logger=logger, proxies={})}
        emit('operation', method='yt-dlp', operation='YoutubeDL.extract_info', url=url, message='Analizando formatos disponibles.', phase='analyzing')
        info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") in {"playlist", "multi_video"}:
            raise DownloadError("El enlace debe identificar un archivo o vídeo individual.")
        if info.get("is_live") or info.get("has_drm"):
            raise DownloadError("No se descargan emisiones en vivo ni contenido con DRM.")
        emit("metadata", title=str(info.get("title", "Descarga"))[:200], duration=info.get("duration"))
        formats = info.get("formats") or []
        info["formats"] = [item for item in formats if item.get("protocol") in {"http", "https", "m3u8_native", "http_dash_segments"}
                           and not item.get("has_drm")]
        if not info["formats"]:
            if info.get("protocol") not in {"http", "https", "m3u8_native", "http_dash_segments"} or info.get("has_drm"):
                raise DownloadError("No hay un formato público compatible disponible.")
        formats = info["formats"] or [info]
        tracks = select_media_formats(ydl, formats, mode, limit)
        if not tracks or len(tracks) > 2:
            raise DownloadError("No se obtuvo un formato multimedia compatible.")
        estimates = [item.get("filesize") or item.get("filesize_approx") or 0 for item in tracks]
        paths = []
        for track_index, track in enumerate(tracks):
            extension = str(track.get("ext", "bin"))
            if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
                extension = "bin"
            path = directory / f"{prefix}-{track_index}.{extension}"
            track_info = {key: value for key, value in info.items() if key not in {"formats", "requested_formats", "requested_downloads"}}
            track_info.update(track)
            emit('operation', method='yt-dlp', operation='YoutubeDL.dl', url=url, message='Descargando pista ' + str(track_index + 1), phase='downloading')
            success, _ = ydl.dl(str(path), track_info)
            if not success or not path.is_file() or not path.stat().st_size:
                raise DownloadError("No se obtuvo una pista multimedia completa.")
            completed += path.stat().st_size
            if completed > limit:
                raise DownloadError("Las pistas del vídeo superan el tamaño permitido.")
            paths.append(path)
        return {"path": paths[0], "title": info.get("title", "descarga"),
                "duration": info.get("duration"),
                "expected_audio": any(item.get("acodec") != "none" for item in tracks),
                **({"audio_path": paths[1]} if len(paths) == 2 else {})}


def probe_media(path):
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        command = [probe, "-v", "error", "-protocol_whitelist", "file,pipe",
                                  "-format_whitelist", "mov,mp3,wav,ogg,flac,matroska,webm,mpegts,avi,aac,asf", "-show_entries",
                                  "format=duration,format_name:stream=codec_type,codec_name", "-of", "json", str(path)]
        emit('operation', method='ffprobe', command=command, message='Comprobando las pistas del archivo.')
        checked = subprocess.run(command,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    except subprocess.TimeoutExpired:
        raise ConversionError("La comprobación multimedia agotó su tiempo; se conserva el original para reintentar.") from None
    if checked.returncode:
        emit('console', method='ffprobe', level='error', message=checked.stderr.decode('utf-8', errors='replace')[-2000:])
        raise ConversionError("La comprobación multimedia detectó un archivo no válido.")
    information = json.loads(checked.stdout)
    tracks = ', '.join(str(item.get('codec_type', '?')) + '/' + str(item.get('codec_name', '?')) for item in information.get('streams', []))
    emit('console', method='ffprobe', level='info', message='Comprobación terminada · salida 0 · pistas: ' + (tracks or 'ninguna'))
    return information


def local_ffmpeg(arguments, duration, deadline, phase="converting"):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ConversionError("Falta FFmpeg para preparar el archivo multimedia.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ConversionError("Se agotó el tiempo de conversión; se conservó el archivo descargado para reintentar.")
    emit("progress", phase=phase, percent=0)
    with tempfile.TemporaryFile() as errors:
        command = [ffmpeg, "-nostdin", "-v", "error", "-y", "-progress", "pipe:1", "-stats_period", "1", *arguments]
        emit('operation', method='ffmpeg', command=command, phase=phase, message='Preparando el archivo con FFmpeg.')
        process = subprocess.Popen(command,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8")
        emit('console', method='ffmpeg', level='info', message='Proceso de preparación iniciado · PID ' + str(process.pid))
        expired = threading.Event()
        def timeout():
            expired.set()
            try:
                process.kill()
            except OSError:
                pass
        timer = threading.Timer(remaining, timeout)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:
                if line.startswith("out_time_us="):
                    value = line.strip().partition("=")[2]
                    if value.isdigit():
                        seconds = int(value) / 1000000
                        emit("progress", phase=phase, seconds=seconds,
                             percent=min(100, round(seconds * 100 / duration, 1)) if duration else None)
            code = process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if expired.is_set():
            raise ConversionError("Se agotó el tiempo de conversión; se conservó el archivo descargado para reintentar.")
        if code:
            errors.seek(max(0, errors.tell() - 2000))
            emit('console', method='ffmpeg', level='error', message='Código de salida ' + str(code) + ': ' + errors.read(2000).decode('utf-8', errors='replace'))
            raise ConversionError("FFmpeg no pudo convertir el archivo; se conservó el original para reintentar.")
        emit('console', method='ffmpeg', level='info', message='Proceso de preparación terminado · PID ' + str(process.pid) + ' · salida 0')


def local_input(path):
    return ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,mp3,wav,ogg,flac,matroska,webm,mpegts,avi,aac,asf", "-i", str(path)]


def convert_audio(path, output, duration, deadline):
    local_ffmpeg([*local_input(path), "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(output)], duration, deadline)


def prepare_video(result, directory, deadline):
    path = result["path"]
    information = probe_media(path)
    if information is None:
        raise ConversionError("Falta ffprobe para comprobar las pistas del vídeo.")
    streams = information.get("streams", [])
    if not any(item.get("codec_type") == "video" for item in streams):
        raise DownloadError("El enlace contiene solo audio; no se encontró un vídeo.")
    audio = result.get("audio_path")
    duration = float(information.get("format", {}).get("duration") or result.get("duration") or 0)
    if audio or "mpegts" in information.get("format", {}).get("format_name", ""):
        audio_information = probe_media(audio) if audio else information
        all_streams = streams + (audio_information or {}).get("streams", [])
        compatible = all(item.get("codec_name") in {"h264", "hevc", "av1", "vp9", "aac", "mp3", "opus", "ac3", "eac3"}
                         for item in all_streams if item.get("codec_type") in {"audio", "video"})
        output = directory / ("video-" + os.urandom(6).hex() + (".mp4" if compatible else ".mkv"))
        arguments = local_input(path)
        if audio:
            arguments += local_input(audio) + ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            arguments += ["-map", "0:v:0", "-map", "0:a:0?"]
        local_ffmpeg([*arguments, "-c", "copy", *(["-movflags", "+faststart"] if compatible else []), str(output)], duration, deadline, "merging")
        path = output
    return path


def finish(result, mode, directory, limit, deadline=None):
    path = result["path"]
    if mode == "video":
        path = prepare_video(result, directory, deadline or time.monotonic() + 1800)
    extension, media_type = signature(path)
    if mode in {"audio", "video"} and not media_type.startswith(("audio/", "video/")):
        raise DownloadError("El archivo recibido no contiene audio o vídeo reconocible.")
    if mode == 'pdf' and media_type != 'application/pdf':
        raise DownloadError('El archivo recibido no es un PDF; se buscará otro enlace disponible.')
    if mode == "audio" and extension != ".mp3":
        information = probe_media(path)
        duration = float((information or {}).get("format", {}).get("duration") or 0)
        output = directory / ("converted-" + os.urandom(6).hex() + ".mp3")
        convert_audio(path, output, duration, deadline or time.monotonic() + 1800)
        path = output
        extension, media_type = signature(path)
    size = path.stat().st_size
    if not 0 < size <= limit:
        raise DownloadError("El archivo está vacío o supera el tamaño permitido.")
    emit("progress", phase="verifying", percent=None)
    if media_type == 'application/pdf':
        emit('operation', method='pymupdf', operation='open', message='Comprobando que el PDF contiene páginas legibles por el lector.')
        try:
            import pymupdf
            with pymupdf.open(path) as document:
                if document.page_count < 1:
                    raise ValueError('PDF sin páginas')
        except Exception:
            raise DownloadError('El archivo no es un PDF válido con páginas; no se entregará como una descarga comprobada.') from None
    information = probe_media(path) if media_type.startswith(("audio/", "video/")) else None
    if information is not None:
        streams = information.get("streams", [])
        expected = mode if mode in {"audio", "video"} else None
        if not streams or expected and not any(item.get("codec_type") == expected for item in streams):
            raise DownloadError("El archivo no contiene la pista solicitada.")
        if mode == "video" and (result.get("expected_audio") or result.get("audio_path")) and not any(item.get("codec_type") == "audio" for item in streams):
            raise ConversionError("El vídeo no conserva su pista de audio; se mantienen los originales para reintentar.")
        if extension == ".webm" and "matroska" in information.get("format", {}).get("format_name", ""):
            with path.open("rb") as stream:
                if b"webm" not in stream.read(128):
                    extension, media_type = ".mkv", "video/x-matroska"
    name = re.sub(r"[^\w .()-]", "_", str(result["title"]), flags=re.UNICODE).strip(" .")[:130] or "descarga"
    name = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name) + extension
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    emit("result", ok=True, filename=name, path=path.name, media_type=media_type, bytes=size, sha256=digest)


def remember_source(result, url, mode, directory):
    path = result["path"]
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    data = {"url": url, "mode": mode, "path": path.name, "title": result["title"], "bytes": path.stat().st_size, "sha256": digest}
    data.update({key: result[key] for key in ("duration", "expected_audio") if key in result})
    if result.get("audio_path"):
        audio = result["audio_path"]
        with audio.open("rb") as stream:
            audio_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        data["audio"] = {"path": audio.name, "bytes": audio.stat().st_size, "sha256": audio_hash}
    temporary = directory / ("source-" + os.urandom(6).hex() + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    temporary.replace(directory / "source.json")


def recalled_source(url, mode, directory):
    try:
        data = json.loads((directory / "source.json").read_text("utf-8"))
        path = (directory / data["path"]).resolve()
        if data["url"] != url or data["mode"] != mode or path.parent != directory.resolve() or not path.is_file() or path.stat().st_size != data["bytes"]:
            return None
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != data["sha256"]:
                return None
        result = {"path": path, "title": data["title"], **{key: data[key] for key in ("duration", "expected_audio") if key in data}}
        if data.get("audio"):
            item = data["audio"]
            audio = (directory / item["path"]).resolve()
            if audio.parent != directory.resolve() or not audio.is_file() or audio.stat().st_size != item["bytes"]:
                return None
            with audio.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    return None
            result["audio_path"] = audio
        return result
    except (OSError, ValueError, KeyError):
        return None


def run(payload):
    socket.socket = PublicSocket
    socket.getaddrinfo = public_records
    url = single_url(payload["url"])
    directory = Path.cwd()
    limit, mode = payload["max_bytes"], payload["mode"]
    deadline = time.monotonic() + payload["timeout_seconds"] - 5
    recalled = recalled_source(url, mode, directory)
    if recalled:
        emit("attempt", method="cached", message="Se reutiliza el archivo descargado; no se vuelve a descargar.")
        try:
            finish(recalled, mode, directory, limit, deadline)
            return
        except ConversionError:
            raise
        except DownloadError:
            # Un candidato del tipo equivocado no debe impedir probar otra fuente.
            (directory / "source.json").unlink(missing_ok=True)
    attempts, candidates, code, blocking = [], [url], "download_failed", None
    retry_after = None
    extractor = matching_extractor(url, mode)
    emit('strategy', method='yt-dlp' if extractor else 'http', extractor=extractor,
         message=('URL reconocida por ' + extractor + ': se usa primero su extractor específico.' if extractor
                  else 'Se examina primero el archivo o los enlaces ofrecidos por la página.'))
    if extractor:
        try:
            emit('attempt', method='yt-dlp', attempt=1, url=url)
            result = media_download(url, directory, mode, limit, deadline)
            remember_source(result, url, mode, directory)
            finish(result, mode, directory, limit, deadline)
            return
        except ConversionError:
            raise
        except Exception as error:
            code, message, detail = failure_message(error)
            attempts.append({'method': 'yt-dlp', 'message': message, 'detail': detail})
            emit('attempt_failed', **attempts[-1])
            if code in {'site_access_required', 'site_rate_limited', 'protected_media', 'download_temporary'}:
                retry_after = retry_after_seconds(error)
                emit('result', ok=False, error=code, message=message, attempts=attempts,
                     **({'retry_after_seconds': retry_after} if retry_after is not None else {}))
                return
            emit('strategy', method='http', message='El extractor no completó el archivo; se comprobará el enlace directo y sus archivos ofrecidos.')
    for candidate in candidates:
        if len(attempts) >= 5 or time.monotonic() > deadline:
            break
        try:
            emit("attempt", method="http", url=candidate)
            emit('operation', method='http', operation='GET', url=candidate, phase='analyzing', message='Examinando el enlace y su tipo de contenido.')
            result = direct(candidate, directory / ("direct-" + os.urandom(6).hex() + ".bin"), limit, mode)
            if "path" in result:
                if mode == 'pdf' and signature(result['path'])[1] != 'application/pdf':
                    raise DownloadError('El enlace entregó otro tipo de archivo; se buscará un PDF.')
                remember_source(result, url, mode, directory)
                finish(result, mode, directory, limit, deadline)
                return
            candidates.extend(link for link in result["links"] if link not in candidates)
            emit('discovery', method='http', phase='analyzing', count=len(result['links']), links=result['links'][:6], message='Página analizada: ' + str(len(result['links'])) + ' enlace(s) compatible(s).')
            attempts.append({"method": "http", "message": "Página HTML examinada; buscando un archivo o extractor."})
        except ConversionError:
            raise
        except Exception as error:
            code, message, detail = failure_message(error)
            attempts.append({"method": "http", "message": message, "detail": detail})
            if code in {'site_rate_limited', 'download_temporary'}:
                retry_after = retry_after_seconds(error)
            emit("attempt_failed", **attempts[-1])
            if code in {'site_access_required', 'site_rate_limited', 'protected_media', 'download_temporary'}:
                blocking = (code, message)
                if candidate == url:
                    break
    # Un PDF no se busca con un extractor de vídeo; tampoco se rodea un bloqueo de acceso.
    if mode == 'pdf' or blocking:
        emit('result', ok=False, error=blocking[0] if blocking else code,
             message=blocking[1] if blocking else (attempts[-1]['message'] if attempts and attempts[-1].get('detail') else 'La página no ofreció un PDF descargable entre los enlaces examinados.'), attempts=attempts,
             **({'retry_after_seconds': retry_after} if retry_after is not None else {}))
        return
    extractor_urls = [url, *[value for value in candidates if value != url]][:3]
    for attempt, extractor_url in enumerate([*extractor_urls, url][:max(2, len(extractor_urls))]):
        if time.monotonic() > deadline:
            break
        try:
            emit("attempt", method="yt-dlp", attempt=attempt + 1, url=extractor_url)
            result = media_download(extractor_url, directory, mode, limit, deadline)
            remember_source(result, url, mode, directory)
            finish(result, mode, directory, limit, deadline)
            return
        except ConversionError:
            raise
        except Exception as error:
            code, message, detail = failure_message(error)
            attempts.append({"method": "yt-dlp", "message": message, "detail": detail})
            if code in {'site_rate_limited', 'download_temporary'}:
                retry_after = retry_after_seconds(error)
            emit("attempt_failed", **attempts[-1])
            if code in {"site_access_required", "site_rate_limited", "protected_media", "unsupported_download_url", "download_temporary"}:
                blocking = (code, message)
                break
    emit("result", ok=False, error=code if attempts else "download_failed",
         message=attempts[-1]["message"] if attempts else "No se pudo completar la descarga con los métodos disponibles.", attempts=attempts,
         **({'retry_after_seconds': retry_after} if retry_after is not None else {}))


if __name__ == "__main__":
    try:
        run(json.load(sys.stdin))
    except ConversionError as error:
        emit("result", ok=False, error="conversion_failed", message=str(error))
    except Exception as error:
        emit("result", ok=False, error="download_failed", message=str(error)[-700:])
