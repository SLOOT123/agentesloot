"""Lector fijo y protecciones de red con DNS/HTTP/Docker simulados; sin red real."""
import asyncio
import ast
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agent_core.config import AgentError, SandboxConfig
from agent_core.sandbox import SandboxRunner
from agent_core.tools import Tools
from agent_core.web_reader import ReaderRunner, WebReader
from agent_core import web_reader_worker as worker
from scripts.setup_search import prepare


class WebReaderTest(unittest.TestCase):
    def test_search_setup_preserves_existing_secrets_and_custom_search_url(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.example").write_text("AGENT_API_KEY=\nSEARXNG_URL=\n", "utf-8")
            path = prepare(root)
            secret = path.read_text("utf-8")
            self.assertIn("http://127.0.0.1:8888", (root / ".env").read_text("utf-8"))
            configured = "AGENT_API_KEY=private-fixture\nSEARXNG_URL=https://custom.example\n"
            (root / ".env").write_text(configured, "utf-8")
            prepare(root)
            self.assertEqual(path.read_text("utf-8"), secret)
            self.assertEqual((root / ".env").read_text("utf-8"), configured)

    def test_dns_rejects_private_mixed_and_special_destinations(self):
        for ips in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "192.168.1.1"],
                    ["::1"], ["::ffff:8.8.8.8"], ["224.0.0.1"], ["2002:a00:1::"], []):
            records = [(2, 1, 6, "", (ip, 443)) for ip in ips]
            with self.subTest(ips=ips), patch.object(worker.socket, "getaddrinfo", return_value=records), self.assertRaises(worker.ReadError):
                worker.public_addresses("test.example")

    def test_connection_pins_ip_and_preserves_tls_hostname(self):
        context, raw = Mock(), Mock()
        with patch.object(worker.ssl, "create_default_context", return_value=context), patch.object(worker.socket, "create_connection", return_value=raw) as connect:
            connection = worker.PinnedHTTPS("docs.example", "8.8.8.8")
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 4)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="docs.example")

    def test_reader_respects_robots_and_does_not_follow_redirects(self):
        with patch.object(worker, "public_addresses", return_value=["8.8.8.8"]):
            with patch.object(worker, "fetch", return_value=(200, "text/plain", b"User-agent: *\nDisallow: /secret")) as fetch:
                with self.assertRaises(worker.ReadError):
                    worker.read_page("https://docs.example/secret")
                self.assertEqual(fetch.call_count, 1)
            for status in (301, 403, 429, 500):
                with patch.object(worker, "fetch", return_value=(status, "text/plain", b"")) as fetch:
                    with self.assertRaises(worker.ReadError):
                        worker.read_page("https://docs.example/page")
                    self.assertEqual(fetch.call_count, 1)
            with patch.object(worker, "fetch", side_effect=[(404, "text/plain", b""), (302, "text/html", b"")]) as fetch:
                with self.assertRaises(worker.ReadError):
                    worker.read_page("https://docs.example/page")
                self.assertEqual(fetch.call_count, 2)

    def test_extraction_is_bounded_and_never_executes_embedded_content(self):
        html = b"<html><head>NO_HEAD</head><script>NO_SCRIPT</script><style>NO_STYLE</style><body><h1>Datos &amp; pruebas</h1>" + b"x" * 1200 + b"</body></html>"
        with patch.object(worker, "public_addresses", return_value=["8.8.8.8"]), patch.object(worker, "fetch", side_effect=[(404, "text/plain", b""), (200, "text/html", html)]):
            result = worker.read_page("https://docs.example/page")
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 1000)
        self.assertIn("Datos & pruebas", result["text"])
        self.assertNotIn("NO_", result["text"])
        for url in ("http://docs.example/", "https://user:pass@docs.example/", "https://docs.example:8443/", "https://localhost/", "https://docs.example/\n"):
            with self.subTest(url=url), self.assertRaises(worker.ReadError):
                worker.read_page(url)

    def test_only_fixed_reader_has_network_and_no_host_mounts(self):
        config = SandboxConfig(enabled=True)
        command = ReaderRunner(config).command("docker", "community-lab-test", "python")
        self.assertIn("--network=bridge", command)
        self.assertIn("--network=none", SandboxRunner(config).command("docker", "community-lab-test", "python"))
        for option in ("--privileged", "--mount", "--volume", "--env-file", "--network=host"):
            self.assertNotIn(option, command)
        reader = WebReader(config)
        reader.runner.run = AsyncMock(return_value={"ok": True, "stdout": '{"ok":true,"text":"Ejemplo","truncated":false}'})
        result = asyncio.run(reader.read("https://docs.example/"))
        self.assertTrue(result["ok"])
        code = reader.runner.run.await_args.args[1]
        self.assertIn("class PinnedHTTPS", code)
        self.assertLessEqual(len(code), ReaderRunner.max_code_chars)
        self.assertEqual(SandboxRunner.max_code_chars, 8000)
        self.assertEqual(ReaderRunner.max_code_chars, 16000)

    def test_large_pages_up_to_one_mib_are_read_but_larger_responses_have_a_safe_code(self):
        self.assertEqual(worker.MAX_BYTES, 1024 * 1024)
        html = b"<main><p>Large page test</p>" + b"x" * 300000 + b"</main>"
        with patch.object(worker, "public_addresses", return_value=["8.8.8.8"]), patch.object(worker, "fetch", side_effect=[(404, "text/plain", b""), (200, "text/html", html)]) as fetch:
            result = worker.read_page("https://docs.example/page")
        self.assertTrue(result["ok"])
        self.assertEqual(result["bytes_read"], len(html))
        self.assertEqual(fetch.call_args.args[-1], 1024 * 1024)
        response = Mock()
        response.getheader.side_effect = lambda key, default="": {"Content-Length": str(worker.MAX_BYTES + 1)}.get(key, default)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(worker, "PinnedHTTPS", return_value=connection), self.assertRaises(worker.ReadError) as oversized:
            worker.fetch("docs.example", "8.8.8.8", "/page", worker.MAX_BYTES)
        self.assertEqual(oversized.exception.code, "page_too_large")
        response.read.assert_not_called()
        connection.close.assert_called_once()
        response.getheader.side_effect = lambda key, default="": default
        response.read.return_value = b"x" * (worker.MAX_BYTES + 1)
        with patch.object(worker, "PinnedHTTPS", return_value=connection), self.assertRaises(worker.ReadError) as streamed:
            worker.fetch("docs.example", "8.8.8.8", "/page", worker.MAX_BYTES)
        self.assertEqual(streamed.exception.code, "page_too_large")
        response.read.assert_called_once_with(worker.MAX_BYTES + 1)

    def test_query_extracts_observed_relevant_passages_and_discards_navigation(self):
        html = ("<html><head>NO_HEAD</head><body><nav>assertRaises NO_NAV</nav>"
                "<div role='navigation'>assertRaises NO_ROLE_NAV</div><aside>assertRaises NO_ASIDE</aside>"
                "<div>assertRaises NO_OUTSIDE_MAIN</div><main><p>" + "Introducción sin coincidencias. " * 100 +
                "</p><h2>assertRaises</h2><p>assertRaises comprueba que se produce una excepción concreta.</p>"
                "<script>raise Exception('NO_EXECUTED')</script>"
                "<p>" + "Más información independiente. " * 120 + "</p>"
                "<p>La excepción esperada se verifica con assertRaises al ejecutar el bloque.</p></main>"
                "<footer>assertRaises NO_FOOTER</footer></body></html>").encode()
        with patch.object(worker, "public_addresses", return_value=["8.8.8.8"]), patch.object(worker, "fetch", side_effect=[(404, "text/plain", b""), (200, "text/html", html)]):
            result = worker.read_page("https://docs.example/page", "¿Cómo comprueba assertRaises una excepción?")
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(result["text"]), 1800)
        self.assertIn("assertRaises comprueba", result["text"])
        self.assertIn("[Pasaje 1]", result["text"])
        self.assertNotIn("NO_", result["text"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["extraction"], "relevant_passages")
        self.assertIn("coincidencia léxica", result["scope"])
        self.assertIn("sin ejecutar JavaScript", result["scope"])
        parser = worker.TextOnly()
        parser.feed(html.decode())
        observed = " ".join(" ".join(parser.main_parts).split())
        for passage in result["text"].split("\n\n"):
            self.assertIn(passage.split("] ", 1)[1], observed)

    def test_lexical_excerpt_is_deterministic_bounded_and_reports_absent_terms(self):
        text = ("unittest " + "contenido " * 100) * 8
        first = worker.relevant_excerpt(text, "unittest")
        self.assertEqual(first, worker.relevant_excerpt(text, "unittest"))
        self.assertLessEqual(len(first[0]), 1800)
        self.assertEqual(worker.relevant_excerpt(text, "missingterm"), (text[:1800], True, False))
        with patch.object(worker, "public_addresses", return_value=["8.8.8.8"]), patch.object(worker, "fetch", side_effect=[(404, "text/plain", b""), (200, "text/html", b"<body role='main'><p>contenido visible</p></body>")]):
            result = worker.read_page("https://docs.example/page", "unittest")
        self.assertIn("No se encontraron coincidencias", result["scope"])
        self.assertEqual(result["text"], "contenido visible")

    def test_diagnostics_preserve_failure_categories_without_network_details(self):
        failures = [(worker.ReadError("robots_denied", "robots.txt no permite la lectura."), "robots_denied"),
                    (TimeoutError("secret-network-detail"), "network_timeout"),
                    (worker.ssl.SSLError("secret-network-detail"), "tls_error"),
                    (OSError("secret-network-detail"), "network_unavailable")]
        for error, expected in failures:
            with self.subTest(code=expected), patch.object(worker, "read_page", side_effect=error), contextlib.redirect_stdout(io.StringIO()) as output:
                worker.main("https://docs.example/page")
                result = json.loads(output.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], expected)
            self.assertNotIn("secret-network-detail", json.dumps(result))
        with patch.object(worker.socket, "getaddrinfo", side_effect=worker.socket.gaierror("secret-dns-detail")), self.assertRaises(worker.ReadError) as dns:
            worker.public_addresses("docs.example")
        self.assertEqual(dns.exception.code, "dns_unavailable")

    def test_query_and_url_are_literals_and_model_scripts_keep_eight_thousand_limit(self):
        reader = WebReader(SandboxConfig(enabled=True))
        reader.query = "'); __import__('os').system('NO_EXECUTION'); #"
        reader.runner.run = AsyncMock(return_value={"ok": True, "stdout": '{"ok":true,"text":"Ejemplo","truncated":false}'})
        asyncio.run(reader.read("https://docs.example/page?q='quoted'"))
        code = reader.runner.run.await_args.args[1]
        self.assertTrue(code.endswith("main(" + repr("https://docs.example/page?q='quoted'") + ", " + repr(reader.query) + ")\n"))
        compile(code, "<fixed-reader>", "exec")
        call = ast.parse(code).body[-1].value
        self.assertIsInstance(call, ast.Call)
        self.assertEqual([argument.value for argument in call.args], ["https://docs.example/page?q='quoted'", reader.query])
        self.assertTrue(all(isinstance(argument, ast.Constant) for argument in call.args))
        self.assertLessEqual(len(code), ReaderRunner.max_code_chars)
        ordinary = SandboxRunner(SandboxConfig(enabled=True))
        self.assertEqual(asyncio.run(ordinary.run("python", "x" * 8001))["error"], "invalid_script")
        self.assertEqual(asyncio.run(reader.runner.__class__(SandboxConfig(enabled=True)).run("python", "x" * 16001))["error"], "invalid_script")

    def test_tool_only_reads_exact_search_sources_and_updates_evidence(self):
        tools = Tools(None, 1, 5)
        tools.add_sources([{"kind": "web", "url": "https://docs.example/page", "title": "Documentación", "excerpt": "Fragmento del buscador"}])
        tools.reader.read = AsyncMock(return_value={"ok": True, "text": "Texto de la página", "truncated": False})

        async def check():
            with self.assertRaises(AgentError):
                await tools.execute("web_read", {"url": "https://otra.example/"}, ["web_read"])
            tools.reader.read.assert_not_awaited()
            result = await tools.execute("web_read", {"url": "https://docs.example/page"}, ["web_read"])
            self.assertEqual(result["source_id"], 1)
            self.assertEqual(tools.sources[0]["retrieval"], "page_extract")
            self.assertEqual(tools.sources[0]["excerpt"], "Texto de la página")
        asyncio.run(check())
