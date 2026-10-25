"""Estado/avisos reales con SQLite y HTTP; red y modelos prohibidos o simulados."""
import asyncio
import hashlib
import io
import json
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
import test_project as fixtures
from agent_core.download_jobs import download_followup, requested_download
from agent_core.downloads import DownloadStore


class DownloadNoticeTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send
    upstream = fixtures.ProjectTest.upstream

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.mode = "failure"

    def job(self, conversation, status="running", message="Descargando archivo."):
        identifier = str(uuid.uuid4())
        with self.app.state.database.connection() as connection:
            connection.execute("INSERT INTO download_jobs(id,conversation_id,url,identity,mode,status,message) VALUES(?,?,?,?,?,?,?)",
                (identifier, conversation, "https://example.org/" + identifier, "https://example.org/" + identifier, "video", status, message))
        return identifier

    def wait_download(self, conversation, identifier):
        for _ in range(150):
            job = self.app.state.download_jobs.get(1, conversation, identifier)
            if job["status"] not in {"running", "queued"}: return job
            time.sleep(.01)
        self.fail("No terminó la descarga simulada")

    def test_followup_and_url_first_commands_are_recognized_without_confusing_file_analysis(self):
        url = "https://www.youtube.com/watch?v=abc"
        self.assertEqual(requested_download(url + " descarga este video"), (url, "video"))
        self.assertIsNone(requested_download(url + " no descargues este video"))
        for text in ("me avisas cuando termine", "Avísame cuando esté listo", "notifícame al terminar"):
            self.assertEqual(download_followup(text), "watch")
        for text in ("¿ya terminó?", "¿Cómo va la descarga?", "estado de las descargas"):
            self.assertEqual(download_followup(text), "status")
        self.assertIsNone(download_followup("Analiza la imagen cuando termine"))
        self.assertIsNone(download_followup("No me avises cuando termine"))

    def test_exact_reported_prompt_skips_images_memory_provider_and_bad_plans(self):
        headers = self.register()
        conversation = self.conversation(headers, "luis")
        image = io.BytesIO(); Image.new("RGB", (60, 40), "red").save(image, "PNG")
        response = self.client.post(f"/api/agent/conversations/{conversation}/attachments", headers=headers, files={"file": ("foto.png", image.getvalue())})
        self.assertEqual(response.status_code, 201, response.text)
        self.job(conversation, "failed", "YouTube exige confirmar el acceso; no se obtuvo un archivo.")
        with patch("agent_core.service.attachment_context", AsyncMock(side_effect=AssertionError("No debe analizar imágenes"))), patch.object(self.app.state.agent, "check", AsyncMock(side_effect=AssertionError("No debe consultar al modelo"))):
            response = self.send(headers, conversation, "me avisas cuando termine")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["download_watch"], [])
        self.assertIn("La descarga no se completó", result["answer"])
        self.assertIn("confirmar el acceso", result["answer"])
        self.assertNotIn("Te avisaré", result["answer"])
        self.assertFalse(any(item["kind"] == "attachment" for item in result["sources"]))
        self.assertFalse(self.requests)

    def test_success_notice_persists_once_with_private_file_link_and_keeps_model_history_in_pairs(self):
        headers = self.register()
        conversation = self.conversation(headers)
        self.release = asyncio.Event()
        async def finish(url, mode, **kwargs):
            await self.release.wait()
            path = self.root / "video.bin"; content = b"video sintetico"; path.write_bytes(content)
            info = {"filename": "video verificado.mp4", "bytes": len(content), "media_type": "video/mp4", "sha256": hashlib.sha256(content).hexdigest()}
            return {"ok": True, "download": DownloadStore(self.app.state.database, 1, conversation).save(path, info, url)}
        with patch("agent_core.download_jobs.Downloader", return_value=SimpleNamespace(run=AsyncMock(side_effect=finish))):
            queued = self.client.post(f"/api/agent/conversations/{conversation}/download-jobs", headers=headers, json={"url": "https://example.org/video", "mode": "video"}).json()
            response = self.send(headers, conversation, "me avisas cuando termine")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Te avisaré en esta conversación", response.json()["answer"])
            self.assertEqual(response.json()["model_calls"], 0)
            self.client.portal.call(self.release.set)
            self.assertEqual(self.wait_download(conversation, queued["job"]["id"])["status"], "complete")
        notices = self.client.get(f"/api/agent/conversations/{conversation}/project").json()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["status"], "complete")
        self.assertIn("video verificado", notices[0]["content"])
        self.assertEqual(self.client.get(notices[0]["download"]["url"]).content, b"video sintetico")
        self.app.state.download_jobs.notify_pending()
        history = self.client.get(f"/api/agent/conversations/{conversation}/messages").json()
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1]["notification_id"], notices[0]["notification_id"])
        self.assertEqual(len(self.app.state.database.history(1, conversation)), 2)
        self.client.portal.call(self.app.state.download_jobs.close)
        self.client.portal.call(self.app.state.download_jobs.start)
        self.assertEqual(len(self.client.get(f"/api/agent/conversations/{conversation}/project").json()["notices"]), 1)
        self.client.cookies.clear(); bob = self.register("bob")
        self.assertEqual(self.client.get(f"/api/agent/conversations/{conversation}/messages").status_code, 404)

    def test_failed_notice_is_recovered_after_restart_without_announcing_success(self):
        headers = self.register()
        conversation = self.conversation(headers)
        identifier = self.job(conversation)
        response = self.send(headers, conversation, "Avísame cuando termine")
        self.assertEqual(response.status_code, 200, response.text)
        self.app.state.download_jobs._update(identifier, status="failed", message="El sitio exige iniciar sesión.", error_code="site_access_required")
        # Simula la caída entre guardar el estado terminal y publicar el aviso.
        self.client.portal.call(self.app.state.download_jobs.close)
        self.client.portal.call(self.app.state.download_jobs.start)
        notices = self.client.get(f"/api/agent/conversations/{conversation}/project").json()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["status"], "failed")
        self.assertIsNone(notices[0]["download"])
        self.assertIn("no se completó", notices[0]["content"])

    def test_other_conversation_cannot_subscribe_to_a_download_and_cancel_generates_one_notice(self):
        headers = self.register()
        first, other = self.conversation(headers), self.conversation(headers)
        identifier = self.job(first)
        result = self.send(headers, other, "me avisas cuando termine")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("No hay descargas", result.json()["answer"])
        self.assertEqual(result.json()["download_watch"], [])
        self.send(headers, first, "me avisas cuando termine")
        response = self.client.post(f"/api/agent/conversations/{first}/download-jobs/{identifier}/cancel", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        notices = self.client.get(f"/api/agent/conversations/{first}/project").json()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["status"], "cancelled")


if __name__ == "__main__": unittest.main()
