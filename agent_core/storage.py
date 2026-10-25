import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import AgentError
from .security import hash_password, token_hash, verify_password
from .retrieval import cosine, terms as retrieval_terms, tfidf_vectors


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA secure_delete=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            legacy = c.execute("SELECT id,password FROM users WHERE password NOT LIKE 'scrypt$%'").fetchall() if exists else []
            if legacy:
                backup_dir = self.path.parent / ".local/backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                with closing(sqlite3.connect(backup_dir / f"before-auth-{stamp}.sqlite3")) as backup:
                    c.backup(backup)
            c.executescript("""
                CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,ip TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT,message TEXT,timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS account_emails(user_id INTEGER PRIMARY KEY REFERENCES users(id),email TEXT NOT NULL COLLATE NOCASE UNIQUE,verified INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS account_tokens(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),purpose TEXT NOT NULL CHECK(purpose IN ('reset','verify')),email TEXT NOT NULL,expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS account_tokens_user ON account_tokens(user_id,purpose);
                CREATE TABLE IF NOT EXISTS rate_limits(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL,resets_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_conversations(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),profile TEXT NOT NULL,backend TEXT NOT NULL,title TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS conversations_user ON agent_conversations(user_id);
                CREATE TABLE IF NOT EXISTS agent_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,sources TEXT NOT NULL DEFAULT '[]',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS agent_messages_conversation ON agent_messages(conversation_id,id);
                CREATE TABLE IF NOT EXISTS agent_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),user_id INTEGER NOT NULL REFERENCES users(id),request_id TEXT NOT NULL,message_hash TEXT NOT NULL,status TEXT NOT NULL,expires_at REAL NOT NULL,response TEXT,error_code TEXT,UNIQUE(conversation_id,request_id));
                CREATE INDEX IF NOT EXISTS agent_runs_active ON agent_runs(status,expires_at);
                CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),title TEXT NOT NULL,content TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS documents_user ON documents(user_id);
                CREATE TABLE IF NOT EXISTS agent_events(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),request_id TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,payload TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS events_run ON agent_events(conversation_id,request_id,id);
                CREATE TABLE IF NOT EXISTS agent_lessons(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),request_id TEXT NOT NULL,attempt_id TEXT NOT NULL,query TEXT NOT NULL,purpose TEXT NOT NULL,error TEXT NOT NULL,language TEXT NOT NULL,code TEXT NOT NULL,output TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'observed' CHECK(status IN ('observed','confirmed','rejected')),created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,conversation_id,request_id,attempt_id));
                CREATE INDEX IF NOT EXISTS lessons_user ON agent_lessons(user_id,status);
                CREATE TABLE IF NOT EXISTS project_files(conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),path TEXT NOT NULL,content TEXT NOT NULL,PRIMARY KEY(conversation_id,path));
                CREATE TABLE IF NOT EXISTS project_checks(conversation_id TEXT PRIMARY KEY REFERENCES agent_conversations(id),manifest TEXT NOT NULL,checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS agent_downloads(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),filename TEXT NOT NULL,media_type TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,source_url TEXT NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS downloads_conversation ON agent_downloads(conversation_id);
                CREATE TABLE IF NOT EXISTS download_jobs(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),request_id TEXT NOT NULL DEFAULT '',url TEXT NOT NULL,identity TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL DEFAULT '',error_code TEXT,progress_json TEXT NOT NULL DEFAULT '{}',result_json TEXT NOT NULL DEFAULT '{}',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,UNIQUE(conversation_id,identity,mode));
                CREATE TABLE IF NOT EXISTS download_watches(job_id TEXT PRIMARY KEY REFERENCES download_jobs(id),conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),request_id TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS download_site_limits(site TEXT PRIMARY KEY,retry_at REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS download_notices(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES download_jobs(id),conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),status TEXT NOT NULL,content TEXT NOT NULL,download_json TEXT NOT NULL DEFAULT 'null',after_message_id INTEGER NOT NULL,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS community_replies(message_id INTEGER PRIMARY KEY REFERENCES messages(id),user_id INTEGER NOT NULL REFERENCES users(id),request_id TEXT NOT NULL,status TEXT NOT NULL,reply_id INTEGER REFERENCES messages(id),error_code TEXT,error_message TEXT NOT NULL DEFAULT '',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,request_id));
                CREATE TABLE IF NOT EXISTS agent_team_checkpoints(conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),request_id TEXT NOT NULL,identity TEXT NOT NULL,phase TEXT NOT NULL,payload TEXT NOT NULL,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(conversation_id,request_id,identity,phase));
                CREATE TABLE IF NOT EXISTS community_team_checkpoints(message_id INTEGER NOT NULL REFERENCES messages(id),identity TEXT NOT NULL,phase TEXT NOT NULL,payload TEXT NOT NULL,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(message_id,identity,phase));
            """)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(messages)")}
            if "role" not in columns:
                c.execute("ALTER TABLE messages ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            if "reply_to" not in columns:
                c.execute("ALTER TABLE messages ADD COLUMN reply_to INTEGER REFERENCES messages(id)")
            if "sources_json" not in columns:
                c.execute("ALTER TABLE messages ADD COLUMN sources_json TEXT NOT NULL DEFAULT '[]'")
            if "team_status" not in columns:
                c.execute("ALTER TABLE messages ADD COLUMN team_status TEXT")
            for user in legacy:
                c.execute("UPDATE users SET password=? WHERE id=?", (hash_password(user["password"] or ""), user["id"]))
            c.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            c.execute('DELETE FROM account_tokens WHERE expires_at<?', (time.time(),))

    def rate(self, bucket: str, maximum: int, seconds: int):
        now = time.time()
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT count,resets_at FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()
            if row and row["resets_at"] > now and row["count"] >= maximum:
                raise AgentError("rate_limit", "Demasiadas solicitudes. Espera un minuto e inténtalo de nuevo.", 429)
            count = row["count"] + 1 if row and row["resets_at"] > now else 1
            end = row["resets_at"] if row and row["resets_at"] > now else now + seconds
            c.execute("INSERT OR REPLACE INTO rate_limits VALUES(?,?,?)", (bucket, count, end))
            c.execute("DELETE FROM rate_limits WHERE resets_at<?", (now - 3600,))

    def authenticate(self, username: str, password: str, register: bool, ip: str, email: str | None = None):
        with self.connection() as c:
            row = c.execute("SELECT id,username,password FROM users WHERE username=?", (username,)).fetchone()
            if register:
                if row:
                    raise AgentError("account_exists", "Ese alias ya está registrado. Usa Iniciar sesión.", 409)
                if not 8 <= len(password) <= 256:
                    raise AgentError("weak_password", "Para una cuenta nueva, usa entre 8 y 256 caracteres de contraseña.", 422)
                try:
                    user_id = c.execute("INSERT INTO users(username,password,ip) VALUES(?,?,?)", (username, hash_password(password), ip)).lastrowid
                    if email is not None:
                        c.execute('INSERT INTO account_emails(user_id,email) VALUES(?,?)', (user_id, email))
                except sqlite3.IntegrityError:
                    raise AgentError("account_exists", "El alias o correo ya está registrado. Inicia sesión o recupera tu cuenta.", 409) from None
                return {"id": user_id, "username": username}
            if not row or not verify_password(password, row["password"]):
                raise AgentError("invalid_credentials", "El alias o la contraseña no son correctos.", 401)
            c.execute("UPDATE users SET ip=? WHERE id=?", (ip, row["id"]))
            return {"id": row["id"], "username": row["username"]}

    def create_session(self, user_id: int, seconds: int):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as c:
            c.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), user_id, csrf, time.time() + seconds))
        return token

    def session(self, token: str | None):
        if not token or len(token) > 128:
            return None
        with self.connection() as c:
            row = c.execute("SELECT u.id,u.username,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (token_hash(token), time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, token: str):
        with self.connection() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def community_messages(self, username: str):
        with self.connection() as c:
            rows = c.execute("""SELECT m.id,m.username,m.message,m.timestamp,m.role,m.reply_to,m.sources_json,m.team_status,
                original.username AS reply_username,j.status AS assistant_status,j.error_message AS assistant_error
                FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 200) m
                LEFT JOIN messages original ON original.id=m.reply_to
                LEFT JOIN community_replies j ON j.message_id=m.id ORDER BY m.id""").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["sources"] = json.loads(item.pop("sources_json"))
                item["current_user"] = username
                result.append(item)
            return result

    def post_community(self, username: str, message: str):
        with self.connection() as c:
            c.execute("INSERT INTO messages(username,message) VALUES(?,?)", (username, message))

    def conversations(self, user_id: int):
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT id,profile,backend,title,created_at FROM agent_conversations WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (user_id,))]

    def conversation(self, user_id: int, conversation_id: str):
        with self.connection() as c:
            row = c.execute("SELECT * FROM agent_conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
            if not row:
                raise AgentError("conversation_not_found", "La conversación no existe.", 404)
            return dict(row)

    def create_conversation(self, user_id: int, profile: str, backend: str):
        conversation_id = str(uuid.uuid4())
        with self.connection() as c:
            c.execute("INSERT INTO agent_conversations(id,user_id,profile,backend,title) VALUES(?,?,?,?,?)", (conversation_id, user_id, profile, backend, "Nueva conversación"))
        return self.conversation(user_id, conversation_id)

    def history(self, user_id: int, conversation_id: str, limit: int = 200):
        self.conversation(user_id, conversation_id)
        with self.connection() as c:
            rows = c.execute("SELECT id,role,content,sources,created_at FROM (SELECT * FROM agent_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id", (conversation_id, limit)).fetchall()
            return [{**dict(row), "sources": json.loads(row["sources"])} for row in rows]

    def start_run(self, user_id: int, conversation_id: str, request_id: str, message: str, timeout: int, maximum: int):
        self.conversation(user_id, conversation_id)
        now = time.time()
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute("SELECT * FROM agent_runs WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            if previous and previous["message_hash"] != token_hash(message):
                raise AgentError("request_conflict", "Ese identificador ya pertenece a otro mensaje.", 409)
            if previous and previous["status"] == "complete":
                return json.loads(previous["response"])
            running = c.execute("SELECT user_id,conversation_id FROM agent_runs WHERE status='running' AND expires_at>?", (now,)).fetchall()
            if any(row["conversation_id"] == conversation_id for row in running):
                raise AgentError("conversation_busy", "Ya hay una respuesta en curso en esta conversación.", 409)
            if len(running) >= maximum or any(row["user_id"] == user_id for row in running):
                raise AgentError("agent_busy", "Hay otra ejecución en curso. Espera a que termine.", 429)
            c.execute("INSERT INTO agent_runs(conversation_id,user_id,request_id,message_hash,status,expires_at) VALUES(?,?,?,?,'running',?) ON CONFLICT(conversation_id,request_id) DO UPDATE SET status='running',expires_at=excluded.expires_at,error_code=NULL", (conversation_id, user_id, request_id, token_hash(message), now + timeout + 15))
            return None

    def cached_run(self, user_id: int, conversation_id: str, request_id: str, message: str):
        self.conversation(user_id, conversation_id)
        with self.connection() as c:
            row = c.execute("SELECT message_hash,status,response FROM agent_runs WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            if row and row["message_hash"] != token_hash(message):
                raise AgentError("request_conflict", "Ese identificador ya pertenece a otro mensaje.", 409)
            return json.loads(row["response"]) if row and row["status"] == "complete" else None

    def finish_run(self, user_id: int, conversation_id: str, request_id: str, message: str, answer: str, sources: list, metadata: dict, lessons=None):
        self.conversation(user_id, conversation_id)
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            user_msg = c.execute("INSERT INTO agent_messages(conversation_id,role,content) VALUES(?,'user',?)", (conversation_id, message)).lastrowid
            assistant_msg = c.execute("INSERT INTO agent_messages(conversation_id,role,content,sources) VALUES(?,'assistant',?,?)", (conversation_id, answer, json.dumps(sources))).lastrowid
            saved_lessons = []
            for lesson in (lessons or [])[:3]:
                if c.execute("SELECT COUNT(*) FROM agent_lessons WHERE user_id=?", (user_id,)).fetchone()[0] >= 200:
                    break
                lesson_id = str(uuid.uuid4())
                c.execute("INSERT INTO agent_lessons(id,user_id,conversation_id,request_id,attempt_id,query,purpose,error,language,code,output) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (lesson_id, user_id, conversation_id, request_id, lesson["attempt_id"], message[:1000], lesson["purpose"], lesson["error"], lesson["language"], lesson["code"], lesson["output"]))
                saved_lessons.append(lesson_id)
            result = {"conversation_id": conversation_id, "user_message_id": user_msg, "assistant_message_id": assistant_msg, "answer": answer, "sources": sources, "lessons_saved": saved_lessons, **metadata}
            c.execute("UPDATE agent_runs SET status='complete',response=? WHERE conversation_id=? AND request_id=?", (json.dumps(result), conversation_id, request_id))
            c.execute("UPDATE agent_conversations SET title=? WHERE id=? AND title='Nueva conversación'", (message[:65], conversation_id))
            return result

    def fail_run(self, conversation_id: str, request_id: str, code: str):
        with self.connection() as c:
            c.execute("UPDATE agent_runs SET status='failed',error_code=? WHERE conversation_id=? AND request_id=? AND status='running'", (code, conversation_id, request_id))

    def documents(self, user_id: int):
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT id,title,created_at,length(content) AS characters FROM documents WHERE user_id=? ORDER BY created_at DESC", (user_id,))]

    def add_document(self, user_id: int, title: str, content: str):
        document_id = str(uuid.uuid4())
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT COUNT(*) FROM documents WHERE user_id=?", (user_id,)).fetchone()[0] >= 50:
                raise AgentError("memory_limit", "La memoria admite 50 documentos por usuario.", 422)
            c.execute("INSERT INTO documents(id,user_id,title,content) VALUES(?,?,?,?)", (document_id, user_id, title, content))
        return document_id

    def delete_document(self, user_id: int, document_id: str):
        with self.connection() as c:
            if not c.execute("DELETE FROM documents WHERE id=? AND user_id=?", (document_id, user_id)).rowcount:
                raise AgentError("document_not_found", "El documento no existe.", 404)

    def search_documents(self, user_id: int, query: str):
        with self.connection() as c:
            rows = c.execute("SELECT id,title,content FROM documents WHERE user_id=? LIMIT 50", (user_id,)).fetchall()
        query_terms = set(retrieval_terms(query))
        query_vector, vectors = tfidf_vectors(query, [row['title'] + ' ' + row['content'] for row in rows])
        ranked = []
        for row, vector in zip(rows, vectors):
            score = cosine(query_vector, vector)
            if score:
                position = next((match.start() for match in re.finditer(r'\w+', row['content'])
                                 if query_terms.intersection(retrieval_terms(match.group()))), 0)
                start = max(0, position - 200)
                ranked.append((score, {"title": row["title"], "url": "", "document_id": row["id"], "kind": "memory", "excerpt": row["content"][start:start + 1400],
                                       "ranking": {"method": "tfidf_cosine_v1", "score": round(score, 6),
                                                   "scope": "Vectores léxicos de documentos del usuario actual; no valida hechos."}}))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:3]]

    def event(self, conversation_id, request_id, kind, title, payload=None):
        with self.connection() as c:
            count = c.execute("SELECT COUNT(*) FROM agent_events WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()[0]
            # Las muestras de consola dejan espacio para los resultados y errores finales.
            if count < (70 if kind == "sandbox_output" else 100):
                c.execute("INSERT INTO agent_events(conversation_id,request_id,kind,title,payload) VALUES(?,?,?,?,?)",
                          (conversation_id, request_id, kind, title[:160], json.dumps(payload or {}, ensure_ascii=False)))

    def activity(self, user_id, conversation_id, request_id=None, after=0):
        self.conversation(user_id, conversation_id)
        with self.connection() as c:
            if request_id:
                run = c.execute("SELECT request_id,status,error_code FROM agent_runs WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            else:
                run = c.execute("SELECT request_id,status,error_code FROM agent_runs WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
            if not run:
                jobs_exist = c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_jobs'").fetchone()
                if jobs_exist:
                    run = c.execute("SELECT request_id,status,error_code FROM agent_jobs WHERE conversation_id=?" + (" AND request_id=?" if request_id else "") + " ORDER BY rowid DESC LIMIT 1", (conversation_id, request_id) if request_id else (conversation_id,)).fetchone()
                if not run:
                    return {"status": "waiting", "events": [], "request_id": request_id}
            events = c.execute("SELECT id,kind,title,payload,created_at FROM agent_events WHERE conversation_id=? AND request_id=? AND id>? ORDER BY id LIMIT 100", (conversation_id, run["request_id"], after)).fetchall()
            return {**dict(run), "events": [{**dict(row), "payload": json.loads(row["payload"])} for row in events]}

    def lessons(self, user_id):
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT id,purpose,error,language,code,output,status,created_at FROM agent_lessons WHERE user_id=? ORDER BY rowid DESC LIMIT 200", (user_id,))]

    def lesson_feedback(self, user_id, lesson_id, verdict):
        status = "confirmed" if verdict == "helpful" else "rejected"
        with self.connection() as c:
            if not c.execute("UPDATE agent_lessons SET status=? WHERE id=? AND user_id=?", (status, lesson_id, user_id)).rowcount:
                raise AgentError("lesson_not_found", "La corrección no existe.", 404)
        return {"status": status}

    def search_lessons(self, user_id, query):
        terms = set(re.findall(r"\w{3,}", query.casefold()))
        with self.connection() as c:
            rows = c.execute("SELECT * FROM agent_lessons WHERE user_id=? AND status!='rejected' ORDER BY rowid DESC LIMIT 200", (user_id,)).fetchall()
        ranked = []
        for row in rows:
            score = sum(term in (row["query"] + " " + row["purpose"] + " " + row["error"]).casefold() for term in terms)
            if score:
                excerpt = (f"Estado: {row['status']}. Fallo observado: {row['error']}\nScript posterior ({row['language']}):\n{row['code']}\nSalida: {row['output']}\n"
                           "Evidencia: ejecución sin errores; precisión general no verificada.")
                ranked.append((score + (2 if row["status"] == "confirmed" else 0),
                               {"kind": "experience", "title": row["purpose"], "lesson_id": row["id"], "url": "", "excerpt": excerpt[:1800]}))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:2]]

    def rejected_lesson_warnings(self, user_id, query):
        terms = set(re.findall(r"\w{3,}", query.casefold()))
        with self.connection() as c:
            rows = c.execute("SELECT purpose,query FROM agent_lessons WHERE user_id=? AND status='rejected' ORDER BY rowid DESC LIMIT 200", (user_id,)).fetchall()
        return [{"purpose": row["purpose"], "status": "experiencia descartada por el usuario"}
                for row in rows if any(term in (row["purpose"] + " " + row["query"]).casefold() for term in terms)][:3]
