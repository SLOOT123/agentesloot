"""Aplicación existente: canal comunitario y agente privado separados."""
import hmac
import html
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit, quote
from uuid import UUID

from fastapi import Depends, FastAPI, Form, Query, Request, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_core.config import AgentError, ConfigFiles, Settings
from agent_core.security import COOKIE_NAME
from agent_core.account_recovery import AccountRecovery, normalize_email
from agent_core.account_routes import install_account_routes, register_page, alert
from agent_core.service import AgentService
from agent_core.storage import Database
from agent_core.projects import ProjectFiles, describe
from agent_core.downloads import DownloadStore
from agent_core.download_jobs import DownloadJobs
from agent_core.community import CommunityAgent
from agent_core.agent_jobs import AgentJobs
from agent_core.attachments import AttachmentStore, SCHEMA as ATTACHMENT_SCHEMA
from agent_core.server_start import SERVICE_NAME, instance_key


class BodyLimitMiddleware:
    def __init__(self, app, maximum=131072):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        uploading = scope["method"] == "POST" and scope.get("path", "").startswith("/api/agent/conversations/") and scope["path"].endswith("/attachments")
        maximum = 21 * 1024 * 1024 if uploading else self.maximum
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > maximum:
                return await JSONResponse({"detail": "El archivo supera 20 MB." if uploading else "La solicitud supera 128 KiB."}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


class TextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=16000)

    @field_validator("message")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Mensaje vacío")
        return value.strip()


class AgentMessage(TextMessage):
    request_id: UUID
    background: bool = False


class CommunityMessage(TextMessage):
    request_id: UUID | None = None


class DownloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2000)
    mode: Literal["audio", "video", "file", "pdf"] = "video"


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str = Field(default="general", max_length=80)
    backend: str | None = Field(default=None, max_length=80)


class ModelCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = Field(max_length=80)


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20000)

    @field_validator("title", "content")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Texto vacío")
        return value.strip()


class LessonFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["helpful", "incorrect"]


def create_app(settings: Settings | None = None, transport=None):
    settings = settings or Settings.from_env()
    database = Database(settings.db_path)
    accounts = AccountRecovery(database, settings)
    files = ConfigFiles(settings.root)
    download_jobs = DownloadJobs(database, files)
    service = AgentService(files, database, transport, download_jobs)
    community_agent = CommunityAgent(database, files, transport)
    agent_jobs = AgentJobs(database, files, service)

    @asynccontextmanager
    async def lifespan(application):
        database.initialize()
        with database.connection() as connection:
            connection.execute(ATTACHMENT_SCHEMA)
        await download_jobs.start()
        await community_agent.start()
        await agent_jobs.start()
        try:
            yield
        finally:
            await agent_jobs.close()
            await community_agent.close()
            await download_jobs.close()

    application = FastAPI(title="IA lovres · Comunidad y agente", lifespan=lifespan)
    application.state.instance_key = instance_key(settings.root, settings.db_path)
    application.state.database = database
    application.state.account_recovery = accounts
    application.state.files = files
    application.state.agent = service
    application.state.download_jobs = download_jobs
    application.state.community_agent = community_agent
    application.state.agent_jobs = agent_jobs
    application.add_middleware(BodyLimitMiddleware)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    application.mount("/static", StaticFiles(directory=str(settings.root / "static")), name="static")

    @application.middleware("http")
    async def browser_security(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlsplit(origin).netloc != request.url.netloc):
                return JSONResponse({"detail": "Origen de solicitud no permitido."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Los formularios POST necesitan conservar Origin para la comprobación CSRF.
        # Los secretos de recuperación se mantienen en fragmentos, nunca en la query.
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'"
        if request.url.path in {"/docs", "/redoc"}:
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(AgentError)
    async def agent_error(request, error):
        return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.status)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "Datos inválidos. Revisa los campos, sus longitudes y el formato.", "code": "invalid_request"}, status_code=422)

    def current_user(request: Request):
        user = database.session(request.cookies.get(COOKIE_NAME))
        if not user:
            raise AgentError("session_required", "Inicia sesión para continuar.", 401)
        page_token = request.headers.get("x-csrf-token")
        if page_token is not None and not hmac.compare_digest(page_token, user["csrf"]):
            raise AgentError("csrf_required", "La cuenta o sesión de esta página cambió. Vuelve a iniciar sesión.", 403)
        return user

    def csrf_user(request: Request, user=Depends(current_user)):
        provided = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(provided, user["csrf"]):
            raise AgentError("csrf_required", "La sesión de la página cambió. Recarga e inténtalo de nuevo.", 403)
        return user

    User = Annotated[dict, Depends(current_user)]
    ProtectedUser = Annotated[dict, Depends(csrf_user)]
    install_account_routes(application, settings, database, accounts)

    @application.get('/healthz', include_in_schema=False)
    def health():
        return {'service': SERVICE_NAME, 'instance': application.state.instance_key, 'status': 'ok'}

    def login_page(error="", status=200, notice=''):
        content = (settings.root / "templates/login.html").read_text("utf-8")
        message = f'<p class="form-error" role="alert">{html.escape(error)}</p>' if error else ""
        return HTMLResponse(content.replace("<!-- LOGIN_ERROR -->", message + alert(notice)), status_code=status)

    @application.get("/", response_class=HTMLResponse)
    async def serve_index(request: Request):
        user = database.session(request.cookies.get(COOKIE_NAME))
        if not user:
            return login_page()
        content = (settings.root / "templates/chat.html").read_text("utf-8")
        return HTMLResponse(content.replace("{{csrf_token}}", html.escape(user["csrf"], quote=True)))

    @application.get("/login", response_class=HTMLResponse)
    async def serve_login(reset: bool = False):
        return login_page(notice='Contraseña actualizada. Inicia sesión con tu nueva contraseña.' if reset else '')

    @application.post("/auth")
    def authenticate(request: Request, username: str = Form(...), password: str = Form(...), action: Literal["login", "register"] = Form("login"), email: str = Form(''), password_confirm: str = Form('')):
        try:
            username = username.strip()
            if not 1 <= len(username) <= 64 or any(ord(char) < 32 for char in username) or not 1 <= len(password) <= 256:
                raise AgentError("invalid_credentials", "Revisa el alias (hasta 64 caracteres) y la contraseña (hasta 256).", 422)
            ip = request.client.host if request.client else "unknown"
            database.rate(f"auth:{ip}", 20, 60)
            if action == 'register':
                email = normalize_email(email)
                if password != password_confirm:
                    raise AgentError('password_mismatch', 'Las contraseñas no coinciden.', 422)
            user = database.authenticate(username, password, action == "register", ip, email=email if action == 'register' else None)
            target = '/'
            if action == 'register':
                notice = 'created'
                if accounts.mailer.configured:
                    try:
                        accounts.send_verification(user['id'])
                        notice = 'verification_sent'
                    except AgentError:
                        notice = 'mail_unavailable'
                target = '/account?notice=' + notice
            token = database.create_session(user["id"], settings.session_seconds)
            old_token = request.cookies.get(COOKIE_NAME)
            if old_token:
                database.logout(old_token)
            response = RedirectResponse(url=target, status_code=303)
            response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.cookie_secure,
                                samesite="strict", max_age=settings.session_seconds, path="/")
            response.delete_cookie("session_user")
            return response
        except AgentError as error:
            if action == 'register':
                return register_page(settings.root, error.message, error.status)
            return login_page(error.message, error.status)

    @application.post("/logout")
    async def logout(request: Request, user: User, csrf_token: str = Form(...)):
        if not hmac.compare_digest(csrf_token, user["csrf"]):
            raise AgentError("csrf_required", "Recarga la página antes de cerrar sesión.", 403)
        database.logout(request.cookies[COOKIE_NAME])
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        response.delete_cookie("session_user")
        return response

    @application.get("/api/session")
    def session_info(user: User):
        return {"username": user["username"], "csrf_token": user["csrf"]}

    @application.get("/api/messages")
    def get_messages(user: User):
        return database.community_messages(user["username"])

    @application.post("/api/messages")
    async def post_message(data: CommunityMessage, user: ProtectedUser):
        if len(data.message) > 8000:
            raise AgentError("message_limit", "El mensaje admite hasta 8000 caracteres.", 422)
        database.rate(f"community:{user['id']}", 60, 60)
        return community_agent.submit(user, data.message, data.request_id)

    @application.get("/api/agent/config")
    def agent_configuration(user: User):
        return files.public()

    @application.get("/api/agent/profiles/{profile_id}/behavior")
    def agent_behavior(profile_id: str, user: User):
        return files.behavior(profile_id)

    @application.post("/api/agent/check")
    async def check_model(data: ModelCheck, user: ProtectedUser):
        database.rate(f"check:{user['id']}", 5, 60)
        return await service.check(data.backend, force=True)

    @application.get("/api/agent/conversations")
    def conversations(user: User):
        return database.conversations(user["id"])

    @application.post("/api/agent/conversations", status_code=201)
    def new_conversation(data: ConversationInput, user: ProtectedUser):
        files.profile(data.profile)
        config = files.agent()
        backend_id = data.backend or config.default_backend
        service.backend(backend_id)
        database.rate(f"conversations:{user['id']}", 30, 60)
        return database.create_conversation(user["id"], data.profile, backend_id)

    @application.get("/api/agent/conversations/{conversation_id}/messages")
    def agent_history(conversation_id: UUID, user: User):
        history = database.history(user["id"], str(conversation_id))
        notices = download_jobs.notices(user["id"], str(conversation_id))
        return sorted([*history, *notices], key=lambda item: (item.get("after_message_id", item["id"]), bool(item.get("notification_id")), item.get("notification_id", 0)))

    @application.post("/api/agent/conversations/{conversation_id}/messages")
    async def agent_message(conversation_id: UUID, data: AgentMessage, user: ProtectedUser):
        if data.background:
            return await agent_jobs.submit(user["id"], str(conversation_id), str(data.request_id), data.message)
        return await service.respond(user["id"], str(conversation_id), str(data.request_id), data.message)

    @application.get("/api/agent/conversations/{conversation_id}/jobs")
    def private_agent_jobs(conversation_id: UUID, user: User):
        return agent_jobs.list(user["id"], str(conversation_id))

    @application.get("/api/agent/conversations/{conversation_id}/jobs/{request_id}")
    def private_agent_job(conversation_id: UUID, request_id: UUID, user: User):
        return agent_jobs.get(user["id"], str(conversation_id), str(request_id))

    @application.post("/api/agent/conversations/{conversation_id}/jobs/{request_id}/cancel")
    async def cancel_private_agent_job(conversation_id: UUID, request_id: UUID, user: ProtectedUser):
        return await agent_jobs.cancel(user["id"], str(conversation_id), str(request_id))

    @application.get("/api/agent/conversations/{conversation_id}/team-artifacts/{request_id}")
    def team_artifacts(conversation_id: UUID, request_id: UUID, user: User):
        database.conversation(user["id"], str(conversation_id))
        import json
        with database.connection() as connection:
            rows = connection.execute("SELECT phase,payload,updated_at FROM agent_team_checkpoints WHERE conversation_id=? AND request_id=? ORDER BY updated_at,phase", (str(conversation_id), str(request_id))).fetchall()
        return {"artifacts": [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]}

    @application.get("/api/agent/memory")
    def memory_documents(user: User):
        return database.documents(user["id"])

    @application.get("/api/agent/conversations/{conversation_id}/project")
    def project_files(conversation_id: UUID, user: User):
        return {"files": describe(ProjectFiles(database, user["id"], str(conversation_id)).read(), content=True),
                "downloads": DownloadStore(database, user["id"], str(conversation_id)).list(),
                "jobs": download_jobs.list(user["id"], str(conversation_id)),
                "notices": download_jobs.notices(user["id"], str(conversation_id))}

    @application.get("/api/agent/conversations/{conversation_id}/attachments")
    def attachment_list(conversation_id: UUID, user: User):
        return {"files": AttachmentStore(database, user["id"], str(conversation_id)).list()}

    @application.post("/api/agent/conversations/{conversation_id}/attachments", status_code=201)
    async def upload_attachment(conversation_id: UUID, user: ProtectedUser, file: UploadFile = File(...)):
        store = AttachmentStore(database, user["id"], str(conversation_id))
        limits = files.agent().attachments
        if not limits.enabled: raise AgentError("attachments_disabled", "La subida de archivos está desactivada.", 422)
        database.rate(f"attachments:{user['id']}", 30, 60)
        try:
            content = await file.read(limits.max_file_mb * 1024 * 1024 + 1)
            return await store.add(file.filename or "archivo.txt", content, limits)
        finally:
            await file.close()

    @application.get("/api/agent/conversations/{conversation_id}/attachments/{attachment_id}/content")
    def attachment_content(conversation_id: UUID, attachment_id: UUID, user: User):
        row = AttachmentStore(database, user["id"], str(conversation_id)).get(str(attachment_id))
        return Response(row["content"], media_type="application/octet-stream", headers={"Cache-Control": "no-store",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["name"], safe=""), "Content-Security-Policy": "sandbox"})

    @application.get("/api/agent/conversations/{conversation_id}/attachments/{attachment_id}/preview")
    def attachment_preview(conversation_id: UUID, attachment_id: UUID, user: User):
        import base64
        row = AttachmentStore(database, user["id"], str(conversation_id)).get(str(attachment_id))
        if not row["preview"]: raise AgentError("attachment_preview_unavailable", "Este archivo no tiene vista previa de imagen.", 404)
        return Response(base64.b64decode(row["preview"]), media_type="image/jpeg", headers={"Cache-Control": "no-store", "Content-Security-Policy": "sandbox"})

    @application.delete("/api/agent/conversations/{conversation_id}/attachments/{attachment_id}")
    def delete_attachment(conversation_id: UUID, attachment_id: UUID, user: ProtectedUser):
        AttachmentStore(database, user["id"], str(conversation_id)).delete(str(attachment_id))
        return {"status": "deleted"}

    @application.post("/api/agent/conversations/{conversation_id}/download-jobs")
    async def start_download(conversation_id: UUID, data: DownloadInput, user: ProtectedUser):
        return await download_jobs.submit(user["id"], str(conversation_id), data.url.strip(), data.mode)

    @application.post("/api/agent/conversations/{conversation_id}/download-jobs/{job_id}/retry")
    async def retry_download(conversation_id: UUID, job_id: UUID, user: ProtectedUser):
        job = download_jobs.get(user["id"], str(conversation_id), str(job_id))
        return await download_jobs.submit(user["id"], str(conversation_id), job["url"], job["mode"], job["request_id"])

    @application.post("/api/agent/conversations/{conversation_id}/download-jobs/{job_id}/cancel")
    async def cancel_download(conversation_id: UUID, job_id: UUID, user: ProtectedUser):
        return await download_jobs.cancel(user["id"], str(conversation_id), str(job_id))

    @application.get("/api/agent/conversations/{conversation_id}/downloads/{download_id}")
    def downloaded_file(conversation_id: UUID, download_id: UUID, user: User):
        item, path = DownloadStore(database, user["id"], str(conversation_id)).get(str(download_id))
        return FileResponse(path, filename=item["filename"], media_type=item["media_type"],
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    @application.get("/api/agent/conversations/{conversation_id}/project.zip")
    def download_project(conversation_id: UUID, user: User):
        content = ProjectFiles(database, user["id"], str(conversation_id)).archive()
        return Response(content, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="proyecto-{conversation_id}.zip"',
            "Cache-Control": "no-store"})

    @application.get("/api/agent/conversations/{conversation_id}/activity")
    def agent_activity(conversation_id: UUID, user: User, request_id: UUID | None = None, after: int = Query(0, ge=0)):
        activity = database.activity(user["id"], str(conversation_id), str(request_id) if request_id else None, after)
        if activity["status"] == "failed" and activity.get("error_code") in {"prompt_too_long", "invalid_prompt"}:
            # El resultado histórico no cambia cuando se repara el archivo del perfil.
            conversation = database.conversation(user["id"], str(conversation_id))
            try:
                files.prompt(files.profile(conversation["profile"]))
                activity["prompt_configuration"] = {"loadable": True}
            except AgentError as error:
                activity["prompt_configuration"] = {"loadable": False, "code": error.code, "message": error.message}
        return activity

    @application.get("/api/agent/lessons")
    def learned_corrections(user: User):
        return database.lessons(user["id"])

    @application.post("/api/agent/lessons/{lesson_id}/feedback")
    def lesson_feedback(lesson_id: UUID, data: LessonFeedback, user: ProtectedUser):
        return database.lesson_feedback(user["id"], str(lesson_id), data.verdict)

    @application.post("/api/agent/memory", status_code=201)
    def save_memory(data: DocumentInput, user: ProtectedUser):
        return {"id": database.add_document(user["id"], data.title, data.content)}

    @application.delete("/api/agent/memory/{document_id}")
    def delete_memory(document_id: UUID, user: ProtectedUser):
        database.delete_document(user["id"], str(document_id))
        return {"status": "deleted"}

    return application


app = create_app()

if __name__ == "__main__":
    from agent_core.server_start import serve
    raise SystemExit(serve(app, host=os.environ.get("APP_HOST", "127.0.0.1"), port=int(os.environ.get("APP_PORT", "8000"))))
