"""Lector fijo HTML/texto para Docker; ninguna instrucción del sitio se ejecuta."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "FashTochLab/1.0"
MAX_BYTES = 1024 * 1024


class ReadError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code)


def public_addresses(host):
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except socket.gaierror:
        raise ReadError("dns_unavailable", "No se pudo resolver el dominio público.") from None
    if not addresses:
        raise ReadError("private_destination", "El destino no es una dirección pública permitida.")
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ReadError("private_destination", "El destino no es público.")
        if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None):
            raise ReadError("private_destination", "No se permite IPv6 de transición.")
    return sorted(addresses, key=lambda ip: ":" in ip)


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=4, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(host, address, path, maximum):
    connection = PinnedHTTPS(host, address)
    try:
        connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ReadError("unsupported_encoding", "La respuesta requiere una compresión no admitida.")
        declared = response.getheader("Content-Length", "")
        if declared.isdigit() and int(declared) > maximum:
            raise ReadError("page_too_large", "La respuesta supera el límite fijo de lectura.")
        body = response.read(maximum + 1)
        if len(body) > maximum:
            raise ReadError("page_too_large", "La respuesta supera el límite fijo de lectura.")
        return response.status, response.getheader("Content-Type", "").lower(), body
    finally:
        connection.close()


class TextOnly(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "template", "head", "nav", "footer", "aside"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "li", "pre", "blockquote", "tr", "br"}

    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.main_parts = [], [], []
        self.base, self.links = base, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        role = attrs.get("role", "").lower()
        hidden = bool(self.stack and self.stack[-1][1]) or tag in self.HIDDEN or role in {"navigation", "contentinfo", "complementary"} or attrs.get("aria-hidden") == "true" or "hidden" in attrs
        primary = bool(self.stack and self.stack[-1][2]) or tag in {"main", "article"} or role == "main"
        if tag not in self.VOID:
            self.stack.append((tag, hidden, primary))
        if not hidden:
            if tag in self.BLOCK:
                self.parts.append("\n")
                if primary:
                    self.main_parts.append("\n")
            if tag in {"a", "audio", "video", "source"}:
                value = attrs.get("href" if tag == "a" else "src")
                if value and len(self.links) < 8:
                    url = urljoin(self.base, value)
                    try:
                        parsed = urlsplit(url)
                        valid = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and parsed.port in {None, 443}
                    except ValueError:
                        valid = False
                    if valid and len(url) <= 600 and not any(item["url"] == url for item in self.links):
                        self.links.append({"url": url, "title": "Enlace de la página"})

    def handle_endtag(self, tag):
        if tag in self.BLOCK and self.stack and not self.stack[-1][1]:
            self.parts.append("\n")
            if self.stack[-1][2]:
                self.main_parts.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)
            if self.stack and self.stack[-1][2]:
                self.main_parts.append(data)


STOP = set("como para por una uno unos unas este esta estos estas con del las los que cual cuales donde cuando sobre entre desde hasta favor quiero necesito busca buscar informacion dame the and for with from this that what how are you your into can about use using".split())


def words(text):
    normalized = "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9_]{3,}", normalized)


def relevant_excerpt(text, query):
    terms = set(word for word in words(query[:1000]) if word not in STOP)
    chunks = []
    for start in range(0, len(text), 500):
        end = min(len(text), start + 600)
        found = set(words(text[start:end])) & terms
        if found:
            chunks.append((len(found), start, end))
    chosen = []
    for score, start, end in sorted(chunks, key=lambda item: (-item[0], item[1])):
        if not any(start < old_end and end > old_start for _, old_start, old_end in chosen):
            chosen.append((score, start, end))
        if len(chosen) == 3:
            break
    if not chosen:
        return text[:1800], len(text) > 1800, False
    passages, covered = [], 0
    for index, (_, start, end) in enumerate(sorted(chosen, key=lambda item: item[1]), 1):
        label = "[Pasaje " + str(index) + "] "
        remaining = 1800 - sum(len(item) for item in passages) - 2 * len(passages) - len(label)
        if remaining <= 0:
            break
        fragment = text[start:min(end, start + remaining)]
        covered += len(fragment)
        passages.append(label + fragment)
    return "\n\n".join(passages), covered < len(text), True


def read_page(url, query=""):
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 32 for c in url):
        raise ReadError("invalid_url", "Usa una URL HTTPS pública válida.")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and parsed.port in {None, 443}
        host = parsed.hostname.encode("idna").decode("ascii") if valid else ""
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ReadError("invalid_url", "Usa HTTPS público:443 sin credenciales.")
    if host == "localhost" or host.endswith((".local", ".localhost")):
        raise ReadError("private_destination", "El destino no es público.")
    address = public_addresses(host)[0]
    path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    status, _, robots = fetch(host, address, "/robots.txt", 65536)
    if status == 200:
        rules = RobotFileParser()
        rules.parse(robots.decode("utf-8", errors="replace").splitlines())
        if not rules.can_fetch(USER_AGENT, url):
            raise ReadError("robots_denied", "robots.txt no permite leer esta página con este agente.")
        delay = rules.crawl_delay(USER_AGENT)
        if delay is not None and delay > 0:
            raise ReadError("robots_delay", "El sitio solicita una pausa; no se consulta automáticamente.")
    elif status not in {404, 410}:
        raise ReadError("robots_unavailable", "No se pudo comprobar robots.txt; no se leyó la página.")
    status, content_type, body = fetch(host, address, path, MAX_BYTES)
    if 300 <= status < 400:
        raise ReadError("page_redirect", "La página redirige a otro destino; el lector no sigue redirecciones.")
    if status != 200:
        raise ReadError("page_http_error", "La página no respondió HTTP 200; no se eluden bloqueos.")
    if not content_type.startswith(("text/html", "text/plain")):
        raise ReadError("unsupported_content", "Solo se extrae HTML o texto.")
    decoded = body.decode("utf-8", errors="replace")
    links, primary = [], False
    if content_type.startswith("text/html"):
        parser = TextOnly(url)
        parser.feed(decoded)
        primary = bool(" ".join(parser.main_parts).strip())
        decoded = " ".join(parser.main_parts if primary else parser.parts)
        links = parser.links
    text = " ".join(decoded.split())
    query = query[:1000] if isinstance(query, str) else ""
    if query.strip():
        excerpt, truncated, matched = relevant_excerpt(text, query)
        scope = ("Pasajes observados seleccionados por coincidencia léxica; hasta 1800 caracteres. " if matched else
                 "No se encontraron coincidencias léxicas; extracto inicial de hasta 1800 caracteres. ")
        scope += "Se priorizó main/article/role=main. " if primary else "Se extrajo texto visible. "
    else:
        excerpt, truncated, matched = text[:1000], len(text) > 1000, False
        scope = "Extracto inicial de hasta 1000 caracteres y enlaces. "
    scope += "Se excluyeron navegación, pie, aside y contenido oculto; lectura parcial sin ejecutar JavaScript ni seguir enlaces automáticamente."
    return {"ok": True, "url": url, "text": excerpt, "truncated": truncated, "bytes_read": len(body),
            "links": links, "scope": scope, "extraction": "relevant_passages" if matched else "leading_excerpt"}


def main(url, query=""):
    try:
        result = read_page(url, query)
    except ReadError as error:
        result = {"ok": False, "error": error.code, "message": error.message}
    except (TimeoutError, socket.timeout):
        result = {"ok": False, "error": "network_timeout", "message": "La conexión pública agotó el tiempo de espera."}
    except ssl.SSLError:
        result = {"ok": False, "error": "tls_error", "message": "No se pudo verificar una conexión TLS segura."}
    except (OSError, http.client.HTTPException):
        result = {"ok": False, "error": "network_unavailable", "message": "No se pudo completar la conexión pública."}
    except (ValueError, UnicodeError):
        result = {"ok": False, "error": "page_unavailable", "message": "No se pudo interpretar la página de forma segura."}
    print(json.dumps(result, ensure_ascii=False))
