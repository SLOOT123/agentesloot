"""Rutas/archivos reales, red y modelo simulados; descarga real en check_download.py."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import io
import shutil
import subprocess
import zipfile
from urllib.error import HTTPError
import pymupdf
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
import test_project as fixtures

from agent_core.config import AgentError
from agent_core import download_worker as worker
from agent_core.downloads import DownloadStore
from agent_core.tools import Tools
from agent_core.download_jobs import requested_download


class DownloadWorkerTest(unittest.TestCase):
    def test_known_media_url_uses_its_extractor_before_a_generic_http_request(self):
        emitted = []
        with tempfile.TemporaryDirectory() as temp, patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
             patch.object(worker.Path, 'cwd', return_value=Path(temp)), patch.object(worker, 'matching_extractor', return_value='youtube'), \
             patch.object(worker, 'recalled_source', return_value=None), patch.object(worker, 'direct') as direct, \
             patch.object(worker, 'media_download', return_value={'path': Path(temp) / 'video.mp4'}) as media, \
             patch.object(worker, 'remember_source'), patch.object(worker, 'finish'), \
             patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
            worker.run({'url': 'https://www.youtube.com/watch?v=v6Ga63b2Q-E', 'mode': 'video', 'max_bytes': 1000, 'timeout_seconds': 30})
        direct.assert_not_called()
        media.assert_called_once()
        self.assertEqual(emitted[0]['kind'], 'strategy')
        self.assertEqual(emitted[0]['extractor'], 'youtube')

    def test_known_extractor_respects_rate_limit_and_does_not_try_http_after_rejection(self):
        emitted = []
        error = HTTPError('https://www.youtube.com/', 429, 'Too Many Requests', {'Retry-After': '600'}, None)
        with tempfile.TemporaryDirectory() as temp, patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
             patch.object(worker.Path, 'cwd', return_value=Path(temp)), patch.object(worker, 'matching_extractor', return_value='youtube'), \
             patch.object(worker, 'recalled_source', return_value=None), patch.object(worker, 'direct') as direct, \
             patch.object(worker, 'media_download', side_effect=error), \
             patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
            worker.run({'url': 'https://www.youtube.com/watch?v=v6Ga63b2Q-E', 'mode': 'video', 'max_bytes': 1000, 'timeout_seconds': 30})
        direct.assert_not_called()
        self.assertEqual(emitted[-1]['error'], 'site_rate_limited')
        self.assertEqual(emitted[-1]['retry_after_seconds'], 600)

    def test_extractor_matching_and_bounded_real_tool_log(self):
        self.assertEqual(worker.matching_extractor('https://www.youtube.com/watch?v=v6Ga63b2Q-E', 'video'), 'youtube')
        self.assertIsNone(worker.matching_extractor('https://example.org/file.pdf', 'pdf'))
        emitted = []
        with patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
            logger = worker.DownloadLogger()
            logger.debug('[debug] internal detail')
            logger.info('\x1b[31m[youtube] Extracting URL\x1b[0m')
            for index in range(100):
                logger.warning('Aviso ' + str(index))
        self.assertEqual(len(emitted), 24)
        self.assertEqual(emitted[0]['message'], '[youtube] Extracting URL')
        self.assertTrue(all(item['kind'] == 'console' for item in emitted))

    def test_pdf_commands_and_recent_page_references_preserve_explicit_intent(self):
        url = 'https://example.org/article'
        for text in ('Descarga el PDF de ' + url, url + ' descarga el pdf', 'Baja el PDF de ' + url):
            self.assertEqual(requested_download(text), (url, 'pdf'))
        history = [{'role': 'user', 'content': 'Mira esta página [fuente](' + url + ')'}, {'role': 'assistant', 'content': 'De acuerdo.'}]
        self.assertEqual(requested_download('Descarga el PDF de esa página', history), (url, 'pdf'))
        self.assertIsNone(requested_download('No descargues el PDF de esa página', history))
        self.assertIsNone(requested_download('¿Qué dice el PDF de esa página?', history))
        self.assertIsNone(requested_download('Descarga el PDF de esa página', history + [{'role': 'user', 'content': 'Estos enlaces https://example.org/a https://example.org/b'}]))

    def test_document_links_include_offered_pdf_buttons_citations_and_embeds(self):
        parser = worker.DocumentLinks('https://example.org/article', pdf_only=True)
        parser.feed('<a href="/login">Entrar</a><a href="/song.mp3" download>Audio</a>'
            '<a href="/download?id=1">Descargar <b>PDF</b></a>'
            '<object data="/embedded?id=2" type="application/pdf"></object>'
            '<meta name="citation_pdf_url" content="/paper.pdf">'
            '<link rel="alternate" type="application/pdf" href="/fulltext">'
            '<iframe src="https://127.0.0.1/private.pdf"></iframe>'
            '<a href="javascript:alert(1)">PDF</a>')
        self.assertEqual(set(parser.links), {'https://example.org/download?id=1', 'https://example.org/embedded?id=2', 'https://example.org/paper.pdf', 'https://example.org/fulltext'})
        self.assertEqual(parser.links[0], 'https://example.org/embedded?id=2')

    def test_office_archives_keep_their_verified_document_format(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'document.bin'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('[Content_Types].xml', '<Types/>')
                archive.writestr('word/document.xml', '<document/>')
            self.assertEqual(worker.signature(path), ('.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'))
            with patch.object(worker, 'small_zip_index', return_value=False):
                self.assertEqual(worker.signature(path), ('.zip', 'application/zip'))

    def test_pdf_is_followed_checked_and_never_sent_to_the_video_extractor(self):
        emitted = []
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp); path = directory / 'downloaded.bin'
            document = pymupdf.open(); document.new_page().insert_text((40, 60), 'PDF de prueba'); document.save(path); document.close()
            with patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
                 patch.object(worker.Path, 'cwd', return_value=directory), \
                 patch.object(worker, 'direct', side_effect=[{'links': ['https://example.org/paper.pdf']}, {'path': path, 'title': 'Artículo.pdf'}]), \
                 patch.object(worker, 'media_download') as media, \
                 patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
                worker.run({'url': 'https://example.org/article', 'mode': 'pdf', 'max_bytes': 100000, 'timeout_seconds': 30})
            media.assert_not_called()
            self.assertTrue(emitted[-1]['ok'])
            self.assertEqual(emitted[-1]['filename'], 'Artículo.pdf')
            self.assertTrue(any(step['kind'] == 'discovery' for step in emitted))
            path.write_bytes(b'%PDF-1.7\nnot a valid document')
            with patch.object(worker, 'emit'), self.assertRaises(worker.DownloadError):
                worker.finish({'path': path, 'title': 'Falso.pdf'}, 'pdf', directory, 100000)

    def test_access_failure_is_preserved_without_running_another_extractor(self):
        emitted = []
        with tempfile.TemporaryDirectory() as temp, patch.object(worker.socket, 'socket'), patch.object(worker.socket, 'getaddrinfo'), \
             patch.object(worker.Path, 'cwd', return_value=Path(temp)), \
             patch.object(worker, 'direct', side_effect=HTTPError('https://example.org/paid', 403, 'Forbidden', {}, None)), \
             patch.object(worker, 'media_download') as media, \
             patch.object(worker, 'emit', side_effect=lambda kind, **item: emitted.append({'kind': kind, **item})):
            worker.run({'url': 'https://example.org/paid', 'mode': 'video', 'max_bytes': 1000, 'timeout_seconds': 30})
        media.assert_not_called()
        self.assertFalse(emitted[-1]['ok'])
        self.assertEqual(emitted[-1]['error'], 'site_access_required')
        self.assertIn('autorizado', emitted[-1]['message'])

    def test_html_sniffing_does_not_download_a_viewer_as_a_document(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        response.url = 'https://example.org/view'; response.status = 200
        response.headers = {'Content-Type': 'application/octet-stream'}
        response.read.side_effect = [b'<html><embed src="/report.pdf" type="application/pdf"></html>', b'']
        with tempfile.TemporaryDirectory() as temp, patch.object(worker, 'build_opener', return_value=Mock(open=Mock(return_value=response))):
            result = worker.direct(response.url, Path(temp) / 'file', 10000, 'pdf')
        self.assertEqual(result['links'], ['https://example.org/report.pdf'])

    def test_explicit_download_links_do_not_require_a_title_or_a_model(self):
        url = "https://www.youtube.com/watch?v=BoUAkO7QpQA&list=radio"
        for text in ("descargas musica de este entace : " + url, "Descarga el audio de " + url, "Quiero descargar esta música: " + url, "¿Puedes descargar el audio de " + url):
            self.assertEqual(requested_download(text), (url, "audio"))
        for text in ("Cómo descargar el audio de " + url, "No descargues " + url, "Analiza " + url):
            self.assertIsNone(requested_download(text))
        self.assertEqual(requested_download(url, [{"role": "user", "content": "quiero descargar una música"}]), (url, "audio"))
        for text in ("Descarga el vídeo con audio de " + url, "Quiero descargar el video de esta canción: " + url):
            self.assertEqual(requested_download(text), (url, "video"))
        self.assertEqual(requested_download("Extrae el audio del vídeo " + url), (url, "audio"))
        self.assertEqual(requested_download("Descarga el video https://example.org/watch?audio=1"), ("https://example.org/watch?audio=1", "video"))

    def test_video_selection_keeps_audio_and_uses_a_smaller_format_within_budget(self):
        from yt_dlp import YoutubeDL
        formats = [
            {"format_id": "audio", "url": "https://example.org/audio", "ext": "m4a", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 100},
            {"format_id": "720", "url": "https://example.org/720", "ext": "mp4", "protocol": "https", "vcodec": "avc1.64001f", "acodec": "none", "height": 720, "filesize": 1000},
            {"format_id": "1080", "url": "https://example.org/1080", "ext": "mp4", "protocol": "https", "vcodec": "avc1.640028", "acodec": "none", "height": 1080, "filesize": 10000},
        ]
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            selected = worker.select_media_formats(ydl, formats, "video", 5000)
            self.assertEqual([item["format_id"] for item in selected], ["720", "audio"])
            with self.assertRaises(worker.DownloadError):
                worker.select_media_formats(ydl, formats[:1], "video", 5000)

    def test_embedded_players_and_manifests_are_discovered_without_selecting_audio_as_video(self):
        parser = worker.MediaLinks("https://example.org/page", "video")
        parser.feed('<audio src="song.mp3"></audio><a href="song.m4a">Song</a><iframe src="https://player.example.org/123"></iframe>'
                    '<meta property="og:video" content="/movie.mp4"><video><source src="/master.m3u8" type="application/x-mpegURL"></video>')
        self.assertEqual(parser.links, ["https://example.org/movie.mp4", "https://example.org/master.m3u8", "https://player.example.org/123"])
        crowded = worker.MediaLinks('https://example.org/page', 'video')
        crowded.feed(''.join('<iframe src="/advert' + str(index) + '"></iframe>' for index in range(8)) + '<video src="/actual.mp4"></video>')
        self.assertEqual(crowded.links[0], 'https://example.org/actual.mp4', 'Los reproductores secundarios no deben desplazar una fuente multimedia explícita.')

    def test_retry_preserves_and_checks_both_video_and_audio_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            video, audio = directory / "video.mp4", directory / "audio.m4a"
            video.write_bytes(b"video bytes")
            audio.write_bytes(b"audio bytes")
            result = {"path": video, "audio_path": audio, "title": "Película", "duration": 20, "expected_audio": True}
            worker.remember_source(result, "https://example.org/video", "video", directory)
            recalled = worker.recalled_source("https://example.org/video", "video", directory)
            self.assertEqual(recalled, result)
            audio.write_bytes(b"other bytes")
            self.assertIsNone(worker.recalled_source("https://example.org/video", "video", directory))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requiere FFmpeg/ffprobe")
    def test_real_video_and_audio_are_merged_and_missing_audio_never_counts_as_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            video, audio = directory / "picture.mp4", directory / "sound.m4a"
            subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10:d=1", "-an", "-c:v", "libx264", str(video)], check=True, capture_output=True, timeout=15)
            subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-vn", "-c:a", "aac", str(audio)], check=True, capture_output=True, timeout=15)
            result = {"path": video, "audio_path": audio, "title": "Vídeo con sonido", "expected_audio": True}
            with patch.object(worker, "emit") as emit:
                worker.finish(result, "video", directory, 1000000, time.monotonic() + 30)
            completed = next(call.kwargs for call in emit.call_args_list if call.args[0] == "result")
            self.assertEqual(completed["media_type"], "video/mp4")
            self.assertEqual({item["codec_type"] for item in worker.probe_media(directory / completed["path"])["streams"]}, {"audio", "video"})
            with patch.object(worker, "emit") as emit, self.assertRaises(worker.ConversionError):
                worker.finish({"path": video, "title": "Vídeo incompleto", "expected_audio": True}, "video", directory, 1000000)
            self.assertFalse(any(call.args[0] == "result" for call in emit.call_args_list))

    def test_conversion_failure_keeps_the_source_and_retry_does_not_download_again(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "media.wav"
            path.write_bytes(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"source audio")
            payload = {"url": "https://example.org/video", "mode": "audio", "max_bytes": 1000, "timeout_seconds": 600}
            with patch.object(worker.socket, "socket"), patch.object(worker.socket, "getaddrinfo"), \
                 patch.object(worker.Path, "cwd", return_value=directory), patch.object(worker, "emit"), \
                 patch.object(worker, "direct", return_value={"links": []}), \
                 patch.object(worker, "media_download", return_value={"path": path, "title": "Audio largo"}) as media, \
                 patch.object(worker, "finish", side_effect=worker.ConversionError("Conversión interrumpida")):
                with self.assertRaises(worker.ConversionError):
                    worker.run(payload)
                media.assert_called_once()
                self.assertTrue((directory / "source.json").exists())
            with patch.object(worker.socket, "socket"), patch.object(worker.socket, "getaddrinfo"), \
                 patch.object(worker.Path, "cwd", return_value=directory), patch.object(worker, "emit"), \
                 patch.object(worker, "direct") as direct, patch.object(worker, "media_download") as media, patch.object(worker, "finish") as finish:
                worker.run(payload)
                direct.assert_not_called()
                media.assert_not_called()
                self.assertEqual(finish.call_args.args[0]["path"], path)

    def test_long_conversion_uses_its_job_deadline_and_reports_media_progress(self):
        process = Mock(stdout=io.StringIO("out_time_us=1800000000\nprogress=continue\n"))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(worker.shutil, "which", return_value="ffmpeg"), patch.object(worker.subprocess, "Popen", return_value=process), \
             patch.object(worker.threading, "Timer") as timer, patch.object(worker, "emit") as emit:
            worker.convert_audio(Path("source.webm"), Path("out.mp3"), 3600, time.monotonic() + 600)
        self.assertGreater(timer.call_args.args[0], 590)
        self.assertTrue(any(call.kwargs.get("percent") == 50 for call in emit.call_args_list))

    def test_private_dns_redirects_ports_and_credential_urls_are_rejected(self):
        for addresses in (["127.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "10.0.0.1"], ["::1"], ["::ffff:8.8.8.8"], []):
            records = [(2, 1, 6, "", (address, 443)) for address in addresses]
            with self.subTest(addresses=addresses), patch.object(worker, "ORIGINAL_GETADDRINFO", return_value=records), self.assertRaises(worker.DownloadError):
                worker.public_records("example.org", 443)
        for url in ("file:///tmp/test", "https://user:pass@example.org/", "https://127.0.0.1/", "http://localhost/", "https://example.org:8443/", "https://example.org/\n"):
            with self.subTest(url=url), self.assertRaises(worker.DownloadError):
                worker.validate_url(url)
        with self.assertRaises(worker.DownloadError):
            worker.PublicRedirect().redirect_request(Mock(full_url="https://example.org/"), None, 302, "", {}, "http://127.0.0.1/private")

    def test_youtube_radio_is_limited_to_the_requested_video(self):
        self.assertEqual(worker.single_url("https://www.youtube.com/watch?v=RvnkAtWcKYg&list=RDRvnkAtWcKYg&start_radio=1"),
                         "https://www.youtube.com/watch?v=RvnkAtWcKYg")

    def test_page_links_are_resolved_and_html_cannot_be_reported_as_mp3(self):
        parser = worker.MediaLinks("https://example.org/music/page")
        parser.feed('<a href="/song.mp3">Audio</a><audio src="track.ogg"></audio><a href="https://127.0.0.1/a.mp3">Private</a><a href="/advert">Ad</a>')
        self.assertEqual(parser.links, ["https://example.org/song.mp3", "https://example.org/music/track.ogg"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fake.mp3"
            path.write_bytes(b"<html>Blocked</html>")
            with self.assertRaises(worker.DownloadError):
                worker.signature(path)

    def test_direct_download_rejects_incomplete_and_oversized_files(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.url = "https://example.org/test.mp3"
        response.status = 200
        response.headers = {"Content-Type": "audio/mpeg", "Content-Length": "50"}
        response.read.side_effect = [b"ID3short", b""]
        with tempfile.TemporaryDirectory() as temp, patch.object(worker, "build_opener", return_value=Mock(open=Mock(return_value=response))):
            with self.assertRaises(worker.DownloadError):
                worker.direct(response.url, Path(temp) / "file", 100)
            with self.assertRaises(worker.DownloadError):
                worker.direct(response.url, Path(temp) / "file", 10)

    def test_worker_follows_a_media_link_after_an_html_page_and_verifies_bytes(self):
        emitted = []
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "real.wav"
            data = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"test audio"
            path.write_bytes(data)
            with patch.object(worker.socket, "socket"), patch.object(worker.socket, "getaddrinfo"), \
                 patch.object(worker.Path, "cwd", return_value=directory), patch.object(worker.shutil, "which", return_value=None), \
                 patch.object(worker, "direct", side_effect=[{"links": ["https://example.org/real.wav"]}, {"path": path, "title": "Prueba"}]) as direct, \
                 patch.object(worker, "media_download") as media, patch.object(worker, "emit", side_effect=lambda kind, **item: emitted.append({"kind": kind, **item})):
                worker.run({"url": "https://example.org/page", "mode": "file", "max_bytes": 1000, "timeout_seconds": 30})
            self.assertEqual(direct.call_count, 2)
            media.assert_not_called()
            result = emitted[-1]
            self.assertTrue(result["ok"])
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())


class DownloadIntegrationTest(unittest.TestCase):
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    tearDown = fixtures.ProjectTest.tearDown

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode, self.model_step, self.failure = "tools", 0, False
        self.app.state.download_jobs.SITE_INTERVAL = 0
        self.url = "https://example.org/audio.mp3"

    def save_download(self, conversation):
        path = self.root / "test-audio.mp3"
        content = b"ID3" + b"fixture audio bytes"
        path.write_bytes(content)
        result = {"filename": "Canción de prueba.mp3", "media_type": "audio/mpeg", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        return DownloadStore(self.app.state.database, 1, conversation).save(path, result, self.url), content

    def wait_job(self, conversation, identifier):
        for _ in range(150):
            data = self.client.get(f"/api/agent/conversations/{conversation}/project").json()
            job = next(item for item in data["jobs"] if item["id"] == identifier)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(.01)
        self.fail("La descarga simulada no terminó")

    async def upstream(self, request):
        if request.url.path != "/api/chat":
            return await fixtures.ProjectTest.upstream(self, request)
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        self.model_step += 1
        if self.model_step == 1:
            content = {"tool": "task_plan", "arguments": {"steps": [{"id": "descargar", "title": "Descargar audio", "tool": "download_url", "depends_on": []}]}}
        elif self.model_step == 2:
            content = {"url": self.url, "mode": "audio"}
        else:
            content = {"answer": "Archivo completado."}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    def test_video_form_queues_the_selected_mode_with_csrf_and_owner_checks(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        endpoint = f"/api/agent/conversations/{conversation}/download-jobs"
        data = {"url": "https://example.org/video", "mode": "video"}
        self.assertEqual(self.client.post(endpoint, json=data).status_code, 403)
        self.assertEqual(self.client.post(endpoint, headers=headers, json={**data, "mode": "wrong"}).status_code, 422)
        self.assertEqual(self.client.post(endpoint, headers=headers, json={**data, "url": "http://localhost/secret"}).status_code, 422)
        runner = SimpleNamespace(run=AsyncMock(return_value={"ok": False, "error": "fixture", "message": "Prueba sin red"}))
        with patch("agent_core.download_jobs.Downloader", return_value=runner):
            response = self.client.post(endpoint, headers=headers, json=data)
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait_job(conversation, response.json()["job"]["id"])
        self.assertEqual(job["mode"], "video")
        self.assertEqual(runner.run.call_args.args, (data["url"], "video"))
        self.assertFalse(self.requests)
        self.client.cookies.clear()
        self.assertEqual(self.client.post(endpoint, headers=headers, json=data).status_code, 401)
        other_headers = self.register("bob")
        self.assertEqual(self.client.post(endpoint, headers=other_headers, json=data).status_code, 404)

    def test_pdf_order_runs_without_model_and_persists_real_tool_steps(self):
        headers = self.register()
        conversation = self.conversation(headers, 'agente')
        async def run(store, event, url, mode, **kwargs):
            self.assertEqual(mode, 'pdf')
            event('download', 'Analizando página', {'kind': 'operation', 'method': 'http', 'operation': 'GET', 'url': url, 'phase': 'analyzing'})
            event('download', 'Enlaces encontrados', {'kind': 'discovery', 'method': 'http', 'links': ['https://example.org/report.pdf'], 'count': 1})
            path = self.root / 'report.pdf'
            document = pymupdf.open(); document.new_page(); document.save(path); document.close()
            data = path.read_bytes()
            record = store.save(path, {'filename': 'report.pdf', 'media_type': 'application/pdf', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}, url)
            return {'ok': True, 'download': record}
        def downloader(store, config, event):
            return SimpleNamespace(run=lambda url, mode, **kwargs: run(store, event, url, mode, **kwargs))
        with patch('agent_core.download_jobs.Downloader', side_effect=downloader):
            answer = self.send(headers, conversation, 'Descarga el PDF de https://example.org/article')
            self.assertEqual(answer.status_code, 200, answer.text)
            job = self.wait_job(conversation, answer.json()['download_job']['id'])
        self.assertFalse(self.requests, 'La orden de descarga no debe depender de inferencias del modelo.')
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['mode'], 'pdf')
        self.assertTrue(any(step.get('operation') == 'GET' for step in job['steps']))
        self.assertTrue(any(step.get('links') == ['https://example.org/report.pdf'] for step in job['steps']))
        self.assertNotIn('steps', job['progress'])
        response = self.client.get(job['download']['url'])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF-'))
        from agent_core.download_jobs import DownloadJobs
        recovered = DownloadJobs(self.app.state.database, self.app.state.agent.files).get(1, conversation, job['id'])
        self.assertEqual(recovered['steps'], job['steps'])

    def test_download_routes_are_private_and_return_original_binary_bytes(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        item, content = self.save_download(conversation)
        response = self.client.get(item["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content)
        self.assertIn("attachment;", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        listed = self.client.get(f"/api/agent/conversations/{conversation}/project").json()["downloads"]
        self.assertEqual(listed[0]["sha256"], hashlib.sha256(content).hexdigest())
        other = self.conversation(headers, "agente")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{other}/downloads/{item['id']}").status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(item["url"]).status_code, 401)
        self.register("bob")
        self.assertEqual(self.client.get(item["url"]).status_code, 404)

    def test_retry_retains_the_failed_steps_before_the_successful_result(self):
        headers = self.register()
        conversation = self.conversation(headers, 'agente')
        count = [0]
        async def run(url, mode, **kwargs):
            count[0] += 1
            if count[0] == 1:
                return {'ok': False, 'error': 'fixture_failed', 'message': 'Fallo inicial conservado'}
            item, _ = self.save_download(conversation)
            return {'ok': True, 'download': item}
        with patch('agent_core.download_jobs.Downloader', return_value=SimpleNamespace(run=run)):
            answer = self.send(headers, conversation, 'Descarga el archivo ' + self.url)
            identifier = answer.json()['download_job']['id']
            first = self.wait_job(conversation, identifier)
            self.assertEqual(first['status'], 'failed')
            response = self.client.post(f'/api/agent/conversations/{conversation}/download-jobs/{identifier}/retry', headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            second = self.wait_job(conversation, identifier)
        self.assertEqual(second['status'], 'complete')
        self.assertTrue(any(step['message'] == 'Fallo inicial conservado' for step in second['steps']))
        self.assertEqual([step['kind'] for step in second['steps'] if step['kind'] in {'attempt_failed', 'retry', 'verified'}], ['attempt_failed', 'retry', 'verified'])

    def test_integrity_failure_never_registers_a_download(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        path = self.root / "changed.mp3"
        path.write_bytes(b"changed")
        store = DownloadStore(self.app.state.database, 1, conversation)
        with self.assertRaises(AgentError) as caught:
            store.save(path, {"filename": "test.mp3", "media_type": "audio/mpeg", "bytes": 7, "sha256": "0" * 64}, self.url)
        self.assertEqual(caught.exception.code, "download_integrity")
        self.assertEqual(store.list(), [])

    def test_tools_accept_user_links_without_search_and_reject_unconsulted_destinations(self):
        headers = self.register()
        conversation = self.conversation(headers, "general")
        tools = Tools(self.app.state.database, 1, 5, conversation_id=conversation)
        tools.user_links("Descarga [audio](" + self.url + ")")
        tools.downloader.run = AsyncMock(return_value={"ok": True})
        asyncio.run(tools.execute("download_url", {"url": self.url, "mode": "audio"}, ["download_url"]))
        tools.downloader.run.assert_awaited_once_with(self.url, "audio")
        repeated = asyncio.run(tools.execute("download_url", {"url": self.url, "mode": "audio"}, ["download_url"]))
        self.assertTrue(repeated["reused"])
        tools.downloader.run.assert_awaited_once()
        with self.assertRaises(AgentError):
            asyncio.run(tools.execute("download_url", {"url": "https://other.example/", "mode": "audio"}, ["download_url"]))
        tools.reader.read = AsyncMock(return_value={"ok": True, "text": "Audio", "truncated": False})
        self.assertTrue(asyncio.run(tools.execute("web_read", {"url": self.url}, ["web_read"]))["ok"])

    def test_explicit_link_starts_background_download_without_asking_for_title(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        async def downloaded(url, mode, **kwargs):
            item, _ = self.save_download(conversation)
            return {"ok": True, "download": item}
        runner = SimpleNamespace(run=AsyncMock(side_effect=downloaded))
        with patch("agent_core.download_jobs.Downloader", return_value=runner):
            response = self.send(headers, conversation, "descargas musica de este entace : " + self.url)
            self.assertEqual(response.status_code, 200, response.text)
            job = self.wait_job(conversation, response.json()["download_job"]["id"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["tools_used"], ["download_url"])
        self.assertEqual(result["review"], "pending_download")
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(self.requests)
        self.assertEqual(job["status"], "complete")
        project = self.client.get(f"/api/agent/conversations/{conversation}/project").json()
        self.assertEqual(project["files"], [])
        self.assertEqual(len(project["downloads"]), 1)

    def test_a_site_block_is_reported_without_claiming_download_success(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        runner = SimpleNamespace(run=AsyncMock(return_value={"ok": False, "error": "site_access_required", "message": "El sitio necesita acceso autorizado.", "attempts": [{"message": "Sign in to confirm you are not a bot"}]}))
        with patch("agent_core.download_jobs.Downloader", return_value=runner):
            response = self.send(headers, conversation, "Descarga el audio de " + self.url)
            job = self.wait_job(conversation, response.json()["download_job"]["id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("acceso autorizado", job["message"])
        self.assertNotIn("Archivo completado", response.json()["answer"])
        runner.run.assert_awaited_once()
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/project").json()["downloads"], [])

    def test_job_continues_after_response_and_duplicate_requests_share_it(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        release = asyncio.Event()
        async def downloaded(url, mode, **kwargs):
            await release.wait()
            item, _ = self.save_download(conversation)
            return {"ok": True, "download": item}
        runner = SimpleNamespace(run=AsyncMock(side_effect=downloaded))
        with patch("agent_core.download_jobs.Downloader", return_value=runner):
            first = self.send(headers, conversation, "Descarga el audio de " + self.url).json()
            second = self.send(headers, conversation, "Descarga el audio de " + self.url).json()
            self.assertEqual(first["download_job"]["id"], second["download_job"]["id"])
            self.assertIn(second["download_job"]["status"], {"queued", "running"})
            self.assertIn("segundo plano", first["answer"])
            self.client.portal.call(release.set)
            self.assertEqual(self.wait_job(conversation, first["download_job"]["id"])["status"], "complete")
        runner.run.assert_awaited_once()

    def test_restart_keeps_the_job_and_staged_source_then_resumes_it(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        release = asyncio.Event()
        stages = []
        async def downloaded(url, mode, *, workdir):
            stages.append(workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "source-kept").write_text("original audio", "utf-8")
            await release.wait()
            item, _ = self.save_download(conversation)
            return {"ok": True, "download": item}
        runner = SimpleNamespace(run=AsyncMock(side_effect=downloaded))
        with patch("agent_core.download_jobs.Downloader", return_value=runner):
            response = self.send(headers, conversation, "Descarga el audio de " + self.url).json()
            manager = self.app.state.download_jobs
            self.client.portal.call(manager.close)
            self.assertTrue((stages[0] / "source-kept").exists())
            self.client.portal.call(manager.start)
            self.client.portal.call(release.set)
            self.assertEqual(self.wait_job(conversation, response["download_job"]["id"])["status"], "complete")
        self.assertEqual(stages[0], stages[1])

    def test_cancellation_and_retry_are_private_and_require_csrf(self):
        headers = self.register()
        conversation = self.conversation(headers, "agente")
        release = asyncio.Event()
        async def downloaded(*args, **kwargs):
            await release.wait()
            item, _ = self.save_download(conversation)
            return {"ok": True, "download": item}
        runner = SimpleNamespace(run=AsyncMock(side_effect=downloaded))
        with patch("agent_core.download_jobs.Downloader", return_value=runner):
            response = self.send(headers, conversation, "Descarga el audio de " + self.url).json()
            identifier = response["download_job"]["id"]
            route = f"/api/agent/conversations/{conversation}/download-jobs/{identifier}"
            self.assertEqual(self.client.post(route + "/cancel").status_code, 403)
            self.assertEqual(self.client.post(route + "/cancel", headers=headers).json()["status"], "cancelled")
            self.assertEqual(self.client.post(route + "/retry").status_code, 403)
            self.assertEqual(self.client.post(route + "/retry", headers=headers).status_code, 200)
            self.client.portal.call(release.set)
            self.assertEqual(self.wait_job(conversation, identifier)["status"], "complete")
        self.client.cookies.clear()
        bob = self.register("bob")
        self.assertEqual(self.client.post(route + "/retry", headers=bob).status_code, 404)
