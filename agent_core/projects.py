"""Archivos de texto privados en SQLite; nunca se escriben en el proyecto anfitrión."""
import hashlib
import io
import json
import re
import zipfile

from .config import AgentError


MAX_FILES, MAX_FILE_BYTES, MAX_PROJECT_BYTES = 32, 16000, 128000
MEDIA_EXTENSIONS = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "opus", "wma", "aiff",
                    "mp4", "webm", "avi", "mov", "mkv", "jpg", "jpeg", "png", "gif", "webp", "ico"}


def validate_files(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise AgentError("invalid_project", 'files debe ser una lista JSON de 1 a 32 objetos, no un texto ni un diccionario. Ejemplo: {"files":[{"path":"ejemplo.py","content":"print(2+2)"}]}', 422)
    checked, names = [], set()
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise AgentError("invalid_project", "Cada archivo necesita path y content.", 422)
        path, content = item["path"], item["content"]
        if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,120}", path)
                or any(part in {"", ".", ".."} or part.endswith(".") or part.split(".")[0].lower() in reserved for part in path.split("/"))
                or path.lower() in names or not isinstance(content, str) or "\x00" in content):
            raise AgentError("invalid_project", "Usa rutas relativas distintas, sin .., con letras, números, guiones y barras /.", 422)
        if path.rpartition(".")[2].lower() in MEDIA_EXTENSIONS:
            raise AgentError("text_files_only", "project_write solo guarda texto y código; no descarga ni genera audio, vídeo o imágenes binarias. No guardes texto con una extensión multimedia. Para una canción, solicita su título/artista y ofrece fuentes; el contenedor no tiene red.", 422)
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise AgentError("project_limit", "Un archivo supera 16000 bytes de texto.", 422)
        names.add(path.lower())
        checked.append({"path": path, "content": content})
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in names for i in range(1, len(parts))):
            raise AgentError("invalid_project", "Una ruta no puede ser archivo y carpeta a la vez.", 422)
    if sum(len(item["content"].encode("utf-8")) for item in checked) > MAX_PROJECT_BYTES:
        raise AgentError("project_limit", "El proyecto supera 128000 bytes.", 422)
    return checked


def describe(files, content=False):
    return [{"path": item["path"], "bytes": len(item["content"].encode("utf-8")),
             "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
             **({"content": item["content"]} if content else {})} for item in files]


class ProjectFiles:
    def __init__(self, database, user_id, conversation_id):
        self.database, self.user_id, self.conversation_id = database, user_id, conversation_id

    def read(self):
        self.database.conversation(self.user_id, self.conversation_id)
        with self.database.connection() as c:
            return [dict(row) for row in c.execute("SELECT path,content FROM project_files WHERE conversation_id=? ORDER BY path", (self.conversation_id,))]

    def write(self, files):
        checked = validate_files(files)
        self.database.conversation(self.user_id, self.conversation_id)
        with self.database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            current = dict(c.execute("SELECT path,content FROM project_files WHERE conversation_id=?", (self.conversation_id,)).fetchall())
            current.update({item["path"]: item["content"] for item in checked})
            validate_files([{"path": path, "content": text} for path, text in current.items()])
            c.executemany("INSERT INTO project_files(conversation_id,path,content) VALUES(?,?,?) ON CONFLICT(conversation_id,path) DO UPDATE SET content=excluded.content",
                          [(self.conversation_id, item["path"], item["content"]) for item in checked])
            c.execute("DELETE FROM project_checks WHERE conversation_id=?", (self.conversation_id,))
        return describe(checked)

    def checked(self):
        self.database.conversation(self.user_id, self.conversation_id)
        with self.database.connection() as c:
            row = c.execute("SELECT manifest FROM project_checks WHERE conversation_id=?", (self.conversation_id,)).fetchone()
            return json.loads(row["manifest"]) if row else []

    def mark_checked(self, files):
        manifest = describe(files)
        self.database.conversation(self.user_id, self.conversation_id)
        with self.database.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            current = [dict(row) for row in c.execute("SELECT path,content FROM project_files WHERE conversation_id=? ORDER BY path", (self.conversation_id,))]
            if describe(current) != manifest:
                raise AgentError("project_changed", "El proyecto cambió durante la prueba; comprueba su versión actual.", 409)
            c.execute("INSERT INTO project_checks(conversation_id,manifest) VALUES(?,?) ON CONFLICT(conversation_id) DO UPDATE SET manifest=excluded.manifest,checked_at=CURRENT_TIMESTAMP",
                      (self.conversation_id, json.dumps(manifest)))

    def archive(self):
        files = self.read()
        if not files:
            raise AgentError("empty_project", "Todavía no hay archivos para descargar.", 404)
        validate_files(files)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.writestr(item["path"], item["content"])
        return output.getvalue()
