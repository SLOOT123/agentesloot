"""Páginas de alta, afiliación de correo y recuperación por enlace temporal."""
import hmac
import html
from fastapi import BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .account_recovery import normalize_email, RESET_NOTICE
from .config import AgentError
from .security import COOKIE_NAME, token_hash


def field(name, label, kind='text', value='', autocomplete='', minimum=None):
    values = {key: html.escape(str(item), quote=True) for key, item in {'name': name, 'label': label, 'kind': kind, 'value': value, 'autocomplete': autocomplete}.items()}
    length = f' minlength="{minimum}"' if minimum else ''
    maximum = 64 if name == 'username' else 254 if kind == 'email' else 256
    result = ('<div class="input-group"><label for="{name}">{label}</label><div class="input-field">'
              '<input id="{name}" name="{name}" type="{kind}" value="{value}" autocomplete="{autocomplete}" required maxlength="' + str(maximum) + '"' + length + '></div>').format(**values)
    if kind == 'password':
        result += f'<button type="button" class="account-password-toggle" data-toggle-password="{values["name"]}" aria-controls="{values["name"]}" aria-pressed="false" hidden>Mostrar contraseña</button>'
    return result + '</div>'


def hidden(name, value):
    return f'<input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}">'


def alert(text, error=False):
    return f'<p class="{"form-error" if error else "account-notice"}" role="{"alert" if error else "status"}">{html.escape(text)}</p>' if text else ''


def page(root, title, subtitle, content, status=200):
    template = (root / 'templates/account.html').read_text('utf-8')
    values = {'title': html.escape(title), 'subtitle': html.escape(subtitle), 'content': content}
    # Una sola sustitución evita interpretar como marcadores el contenido del usuario.
    import re
    rendered = re.sub(r'\{\{(title|subtitle|content)\}\}', lambda match: values[match[1]], template)
    return HTMLResponse(rendered, status_code=status)


def register_page(root, error='', status=200):
    form = (alert(error, True) + '<form class="login-form account-form" action="/auth" method="post">' + hidden('action', 'register')
            + field('username', 'Usuario o alias', autocomplete='username')
            + field('email', 'Correo de recuperación', 'email', autocomplete='email')
            + field('password', 'Contraseña', 'password', autocomplete='new-password', minimum=8)
            + field('password_confirm', 'Repite la contraseña', 'password', autocomplete='new-password', minimum=8)
            + '<p class="field-help">Te pediremos confirmar tu correo para poder recuperar la cuenta.</p>'
            + '<button class="btn-primary" type="submit">Crear cuenta</button></form>'
            + '<a class="account-link" href="/login">Ya tengo una cuenta · Iniciar sesión</a>')
    return page(root, 'Crea tu cuenta', 'Un espacio propio y una forma de recuperar tu acceso.', form, status)


def install_account_routes(application, settings, database, accounts):
    def owner(request, csrf_token=None):
        user = database.session(request.cookies.get(COOKIE_NAME))
        if not user:
            raise AgentError('session_required', 'Inicia sesión para continuar.', 401)
        if csrf_token is not None and not hmac.compare_digest(csrf_token, user['csrf']):
            raise AgentError('csrf_required', 'Recarga la página antes de guardar los cambios.', 403)
        return user

    def account_page(user, message='', error=False, status=200):
        profile = accounts.profile(user['id'])
        verified = profile['verified']
        body = alert(message, error)
        if not accounts.mailer.configured:
            body += alert('El envío de correos aún no está habilitado. Puedes guardar tu correo; la confirmación y recuperación estarán disponibles cuando se active.')
        body += '<p class="account-status">' + ('Correo confirmado · Recuperación habilitada' if verified else 'Correo pendiente de confirmar' if profile['email'] else 'Todavía no afiliaste un correo') + '</p>'
        if profile['email'] and not verified:
            body += ('<form class="account-actions" action="/account/email/resend" method="post">' + hidden('csrf_token', user['csrf'])
                     + '<button class="btn-secondary" type="submit">Enviar enlace de confirmación</button></form>')
        body += ('<form class="login-form account-form" action="/account/email" method="post">' + hidden('csrf_token', user['csrf'])
                 + field('email', 'Correo de recuperación', 'email', profile['email'], 'email')
                 + field('current_password', 'Contraseña actual', 'password', autocomplete='current-password')
                 + '<p class="field-help">Al cambiar el correo tendrás que confirmar la nueva dirección.</p>'
                 + '<button class="btn-primary" type="submit">Guardar correo</button></form><a class="account-link" href="/">Volver al chat</a>')
        return page(settings.root, 'Mi cuenta', 'Afilia un correo y confirma que tienes acceso a él.', body, status)

    def token_page(purpose, error='', status=200, token=''):
        reset = purpose == 'reset'
        path = '/reset-password' if reset else '/verify-email'
        body = alert(error, True) + '<form class="login-form account-form" action="' + path + '" method="post">' + hidden('token', token)
        body += '<p id="tokenHelp" class="form-error" hidden>Abre el enlace que recibiste por correo para continuar.</p>'
        if reset:
            body += field('password', 'Nueva contraseña', 'password', autocomplete='new-password', minimum=8)
            body += field('password_confirm', 'Repite la nueva contraseña', 'password', autocomplete='new-password', minimum=8)
        body += '<button class="btn-primary" type="submit">' + ('Guardar nueva contraseña' if reset else 'Confirmar mi correo') + '</button></form>'
        body += '<a class="account-link" href="/forgot-password">Solicitar otro enlace de recuperación</a><a class="account-link" href="/login">Volver a iniciar sesión</a>'
        return page(settings.root, 'Restablece tu contraseña' if reset else 'Confirma tu correo', 'El enlace solo puede utilizarse una vez.', body, status)

    @application.get('/register', response_class=HTMLResponse)
    def registration():
        return register_page(settings.root)

    @application.get('/account', response_class=HTMLResponse)
    def account(request: Request, notice: str = ''):
        if not database.session(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse('/login', status_code=303)
        messages = {'created': 'Tu cuenta ya está creada. Confirma tu correo para habilitar la recuperación.',
                    'verification_sent': 'Tu cuenta ya está creada. Te enviamos un enlace para confirmar el correo.',
                    'mail_unavailable': 'Tu cuenta ya está creada. No se pudo enviar la confirmación; puedes reenviarla desde aquí.'}
        return account_page(owner(request), messages.get(notice, ''))

    @application.post('/account/email', response_class=HTMLResponse)
    def save_email(request: Request, email: str = Form(...), current_password: str = Form(...), csrf_token: str = Form(...)):
        user = owner(request, csrf_token)
        try:
            database.rate('account-email:' + str(user['id']), 5, 900)
            accounts.set_email(user['id'], current_password, email)
            if accounts.profile(user['id'])['verified']:
                return account_page(user, 'Este correo ya está confirmado.')
            if not accounts.mailer.configured:
                return account_page(user, 'Correo guardado. La confirmación queda pendiente hasta que se habilite el envío.')
            accounts.send_verification(user['id'])
            return account_page(user, 'Correo guardado. Revisa tu bandeja para confirmar la dirección.')
        except AgentError as error:
            return account_page(user, error.message, True, error.status)

    @application.post('/account/email/resend', response_class=HTMLResponse)
    def resend_email(request: Request, csrf_token: str = Form(...)):
        user = owner(request, csrf_token)
        try:
            accounts.send_verification(user['id'])
            return account_page(user, 'Te enviamos un nuevo enlace. Revisa también la carpeta de spam.')
        except AgentError as error:
            return account_page(user, error.message, True, error.status)

    @application.get('/forgot-password', response_class=HTMLResponse)
    def forgot_password():
        body = ('<form class="login-form account-form" action="/forgot-password" method="post">'
                + field('email', 'Correo afiliado a tu cuenta', 'email', autocomplete='email')
                + '<button class="btn-primary" type="submit">Enviar enlace de recuperación</button></form>'
                + '<p class="field-help">Usa el correo que confirmaste en tu cuenta. El enlace tendrá una vigencia de 30 minutos.</p>'
                + '<a class="account-link" href="/login">Volver a iniciar sesión</a>')
        if not accounts.mailer.configured:
            body = alert('El envío de correos aún no está habilitado. Contacta con la administración para activar la recuperación.') + body
        return page(settings.root, 'Recupera tu cuenta', 'Recibe un enlace para crear una nueva contraseña.', body)

    @application.post('/forgot-password', response_class=HTMLResponse)
    def request_reset(request: Request, background: BackgroundTasks, email: str = Form(...)):
        try:
            database.rate('forgot-ip:' + (request.client.host if request.client else 'unknown'), 10, 900)
            email = normalize_email(email)
            database.rate('forgot-email:' + token_hash(email), 3, 900)
            accounts.mailer.require_configured()
            background.add_task(accounts.send_reset, email)
            return page(settings.root, 'Revisa tu correo', RESET_NOTICE, '<a class="account-link" href="/login">Volver a iniciar sesión</a>')
        except AgentError as error:
            return page(settings.root, 'Recupera tu cuenta', 'Solicita un enlace para recuperar tu acceso.', alert(error.message, True) + '<a class="account-link" href="/forgot-password">Volver a intentarlo</a>', error.status)

    @application.get('/reset-password', response_class=HTMLResponse)
    def reset_page():
        return token_page('reset')

    @application.post('/reset-password', response_class=HTMLResponse)
    def reset_password(request: Request, token: str = Form(...), password: str = Form(...), password_confirm: str = Form(...)):
        try:
            database.rate('reset-ip:' + (request.client.host if request.client else 'unknown'), 20, 900)
            if password != password_confirm:
                raise AgentError('password_mismatch', 'Las contraseñas no coinciden.', 422)
            accounts.consume(token, 'reset', password)
            response = RedirectResponse('/login?reset=1', status_code=303)
            response.delete_cookie(COOKIE_NAME)
            return response
        except AgentError as error:
            return token_page('reset', error.message, error.status, token if len(token) <= 128 else '')

    @application.get('/verify-email', response_class=HTMLResponse)
    def verification_page():
        return token_page('verify')

    @application.post('/verify-email', response_class=HTMLResponse)
    def verify_email(request: Request, token: str = Form(...)):
        try:
            database.rate('verify-ip:' + (request.client.host if request.client else 'unknown'), 20, 900)
            accounts.consume(token, 'verify')
            return page(settings.root, 'Correo confirmado', 'Ya puedes utilizar este correo para recuperar tu cuenta.', '<a class="btn-primary" href="/">Continuar</a>')
        except AgentError as error:
            return token_page('verify', error.message, error.status)
