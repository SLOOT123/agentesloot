"""Subidas/extractores/API reales; proveedor y buscador simulados."""
import base64
import io
import json
import unittest
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pymupdf
from PIL import Image
import test_project as fixtures

from agent_core.attachments import AttachmentStore
from agent_core.tools import Tools
from agent_core.providers import ModelTurn


def photo():
    stream = io.BytesIO()
    Image.new("RGB", (100, 80), "red").save(stream, "PNG")
    return stream.getvalue()


def pdf(text="INFORME PRIVADO. El total es 452 euros.", scan=False):
    document = pymupdf.open()
    page = document.new_page()
    if scan: page.insert_image(pymupdf.Rect(30, 30, 300, 250), stream=photo())
    else: page.insert_text((50, 70), text)
    data = document.tobytes()
    document.close()
    return data


def office(name, body):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive: archive.writestr(name, body)
    return stream.getvalue()


class AttachmentTest(unittest.TestCase):
    setUp = fixtures.ProjectTest.setUp
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    conversation = fixtures.ProjectTest.conversation
    send = fixtures.ProjectTest.send

    async def upstream(self, request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/show" and body.get("model") == "qwen3-vl:2b-instruct":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"], "model_info": {"qwen.context_length": 16384}})
        if request.url.path == "/api/chat" and body.get("model") == "qwen3-vl:2b-instruct":
            self.requests.append((request.url.path, body, dict(request.headers)))
            return httpx.Response(200, json={"message": {"content": "OBSERVACION_VISUAL_SIMULADA: se ve un rectángulo rojo."}})
        return await fixtures.ProjectTest.upstream(self, request)

    def upload(self, headers, conversation, name, content, media="application/octet-stream"):
        return self.client.post(f"/api/agent/conversations/{conversation}/attachments", headers=headers, files={"file": (name, content, media)})

    def test_long_markdown_is_searchable_in_chat_without_pasting_or_web_search(self):
        headers = self.register()
        conversation = self.conversation(headers)
        text = "Texto sin relación.\n" * 7000 + "\nCLAVE_PRIVADA_983: La entrega acordada es el martes.\n"
        response = self.upload(headers, conversation, "plan.md", text.encode())
        self.assertEqual(response.status_code, 201, response.text)
        saved = response.json()
        answer = self.send(headers, conversation, "¿Qué dice CLAVE_PRIVADA_983 sobre la entrega?")
        self.assertEqual(answer.status_code, 200, answer.text)
        sources = answer.json()["sources"]
        self.assertEqual(sources[0]["kind"], "attachment")
        self.assertIn("el martes", sources[0]["excerpt"])
        requests = [item[1] for item in self.requests if item[0] == "/api/chat"]
        self.assertIn("CLAVE_PRIVADA_983", json.dumps(requests))
        self.assertTrue(all("web_search" not in json.dumps(item.get("tools", [])) for item in requests))
        self.assertNotIn("/search", [item[0] for item in self.requests])
        self.assertEqual(answer.json()["attachments_used"], [saved["id"]])

    def test_pdf_text_image_preview_and_common_office_formats_are_actually_extracted(self):
        headers = self.register()
        conversation = self.conversation(headers)
        cases = [
            ("informe.pdf", pdf(), "El total es 452"),
            ("notas.docx", office("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Documento de prueba</w:t></w:r></w:p></w:body></w:document>'), "Documento de prueba"),
            ("tabla.xlsx", office("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>Ventas</t></is></c><c r="B1"><v>25</v></c></row></sheetData></worksheet>'), "B1=25"),
            ("charla.pptx", office("ppt/slides/slide1.xml", '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Resumen diapositiva</a:t></p:sld>'), "Resumen diapositiva"),
            ("datos.csv", b"nombre,valor\nventas,25", "ventas,25"),
        ]
        store = AttachmentStore(self.app.state.database, 1, conversation)
        for name, content, expected in cases:
            with self.subTest(name=name):
                response = self.upload(headers, conversation, name, content)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertIn(expected, store.get(response.json()["id"])["extracted_text"])
        response = self.upload(headers, conversation, "foto.png", photo(), "image/png")
        self.assertEqual(response.status_code, 201, response.text)
        base = f"/api/agent/conversations/{conversation}/attachments/{response.json()['id']}"
        preview = self.client.get(base + "/preview")
        self.assertEqual(preview.headers["content-type"], "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(preview.content)).size, (100, 80))
        self.assertEqual(self.client.get(base + "/content").content, photo())

    def test_real_images_are_passed_to_the_visual_provider_and_observations_to_chat(self):
        headers = self.register()
        conversation = self.conversation(headers)
        uploaded = self.upload(headers, conversation, "foto.png", photo()).json()
        answer = self.send(headers, conversation, "¿Qué color aparece en la foto?")
        self.assertEqual(answer.status_code, 200, answer.text)
        source = answer.json()["sources"][0]
        self.assertEqual(source["retrieval"], "vision_model")
        self.assertIn("OBSERVACION_VISUAL_SIMULADA", source["excerpt"])
        visual = [item[1] for item in self.requests if item[0] == "/api/chat" and item[1]["model"] == "qwen3-vl:2b-instruct"]
        images = visual[0]["messages"][-1]["images"]
        self.assertEqual(len(images), 1)
        Image.open(io.BytesIO(base64.b64decode(images[0]))).verify()
        main = [item[1] for item in self.requests if item[0] == "/api/chat" and item[1]["model"] != "qwen3-vl:2b-instruct"]
        self.assertIn("OBSERVACION_VISUAL_SIMULADA", json.dumps(main))
        self.assertNotIn("images", main[0]["messages"][-1])
        self.assertEqual(answer.json()["attachments_used"], [uploaded["id"]])

    def test_scanned_pdf_renders_pages_for_vision_and_reports_scope(self):
        headers = self.register()
        conversation = self.conversation(headers)
        uploaded = self.upload(headers, conversation, "escaneado.pdf", pdf(scan=True)).json()
        self.assertEqual(uploaded["metadata"]["scanned_pages"], [0])
        answer = self.send(headers, conversation, "Describe la página 1 del PDF")
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["sources"][0]["visual_pages"], [1])

    def test_another_user_or_conversation_cannot_read_preview_remove_or_use_private_file(self):
        alice = self.register()
        conversation = self.conversation(alice)
        file = self.upload(alice, conversation, "privado.txt", b"SECRETO_ARCHIVO_ALICE").json()
        base = f"/api/agent/conversations/{conversation}/attachments"
        other = self.conversation(alice)
        foreign = f"/api/agent/conversations/{other}/attachments/{file['id']}/content"
        self.assertEqual(self.client.get(foreign).status_code, 404)
        self.assertNotIn("SECRETO_ARCHIVO_ALICE", self.send(alice, other, "¿Qué dicen los archivos?").text)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(base).status_code, 401)
        self.assertEqual(self.client.get(base + f"/{file['id']}/content").status_code, 401)
        bob = self.register("bob")
        for suffix in ("", f"/{file['id']}/content", f"/{file['id']}/preview"):
            response = self.client.get(base + suffix)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.client.delete(base + f"/{file['id']}", headers=bob).status_code, 404)
        self.assertEqual(self.upload(bob, conversation, "intruso.txt", b"dato").status_code, 404)

    def test_csrf_corruption_duplicate_and_active_job_guards(self):
        headers = self.register()
        conversation = self.conversation(headers)
        self.assertEqual(self.upload({}, conversation, "nota.txt", b"texto").status_code, 403)
        self.assertEqual(self.upload(headers, conversation, "foto.png", b"no es una imagen").status_code, 422)
        self.assertEqual(self.upload(headers, conversation, "script.exe", b"MZ0123").status_code, 422)
        one = self.upload(headers, conversation, "nota.txt", b"datos privados").json()
        two = self.upload(headers, conversation, "otra.txt", b"datos privados").json()
        self.assertEqual(one["id"], two["id"])
        database = self.app.state.database
        database.start_run(1, conversation, str(uuid.uuid4()), "trabajo", 30, 2)
        self.assertEqual(self.upload(headers, conversation, "nota2.txt", b"otros datos").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/agent/conversations/{conversation}/attachments/{one['id']}", headers=headers).status_code, 409)

    def test_more_than_three_visual_views_are_explicitly_partial(self):
        headers = self.register()
        conversation = self.conversation(headers)
        for index in range(4):
            stream = io.BytesIO(); Image.new("RGB", (90 + index, 80), "red").save(stream, "PNG")
            self.assertEqual(self.upload(headers, conversation, f"imagen{index}.png", stream.getvalue()).status_code, 201)
        answer = self.send(headers, conversation, "Analiza todas las imágenes")
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["review"], "unresolved")
        self.assertIn("No se analizaron todas", answer.json()["answer"])
        self.assertEqual(len([item for item in self.requests if item[0] == "/api/chat" and item[1]["model"] == "qwen3-vl:2b-instruct"]), 3)

    def test_twenty_files_fit_luis_context_and_the_reader_can_reach_later_passages(self):
        headers = self.register()
        conversation = self.conversation(headers, "luis")
        for index in range(20):
            content = (f"clave{index}: documento privado {index}.\n" * 70).encode()
            response = self.upload(headers, conversation, "nombre-largo-" + str(index) + "x" * 80 + ".md", content)
            self.assertEqual(response.status_code, 201, response.text)
        self.mode = "tools"
        with patch("agent_core.providers.Provider.structured_action", AsyncMock(return_value=ModelTurn("Datos del archivo [1]"))):
            answer = self.send(headers, conversation, "¿Qué dice clave19 en el documento?")
        self.assertEqual(answer.status_code, 200, answer.text)
        source = answer.json()["sources"][0]
        self.assertIn("clave19", source["excerpt"])
        store = AttachmentStore(self.app.state.database, 1, conversation)
        tools = Tools(self.app.state.database, 1, 5, conversation_id=conversation)
        tools.attachments = store
        read = self.client.portal.call(tools._execute, "attachment_read", {"attachment_id": source["attachment_id"], "offset": 1800})
        self.assertIn("clave19", read["text"])
        self.assertEqual(tools.sources[0]["offset"], 1800)

    def test_vision_failure_is_reported_and_oversized_json_keeps_original_limit(self):
        headers = self.register()
        conversation = self.conversation(headers)
        self.upload(headers, conversation, "foto.png", photo())
        config = self.config(); config["attachments"] = {"vision_backend": "no_configurado"}; self.save_config(config)
        result = self.send(headers, conversation, "Analiza la foto")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("no se pudo completar el análisis visual", result.json()["answer"])
        self.assertEqual(result.json()["review"], "unresolved")
        response = self.client.post(f"/api/agent/conversations/{conversation}/messages", content="x" * 140000, headers={**headers, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_encrypted_pdf_file_size_and_expanded_office_limits(self):
        headers = self.register()
        conversation = self.conversation(headers)
        document = pymupdf.open(); document.new_page()
        locked = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="clave", owner_pw="propietario")
        document.close()
        response = self.upload(headers, conversation, "bloqueado.pdf", locked)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("contraseña", response.json()["detail"])
        giant_xml = office("word/document.xml", "x" * 10_000_001)
        response = self.upload(headers, conversation, "expandido.docx", giant_xml)
        self.assertEqual(response.status_code, 422, response.text)
        config = self.config(); config["attachments"] = {"max_file_mb": 1}; self.save_config(config)
        response = self.upload(headers, conversation, "grande.txt", b"a" * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413, response.text)


if __name__ == "__main__": unittest.main()
