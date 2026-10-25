"""Correo confirmado y enlaces de un uso; los tokens solo se guardan como hash."""
import logging
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

from .config import AgentError
from .security import hash_password, token_hash, verify_password


INVALID_LINK = 'El enlace no es válido, ya se utilizó o ha caducado. Solicita uno nuevo.'
RESET_NOTICE = 'Si el correo está asociado a una cuenta y fue confirmado, recibirás un enlace para recuperar el acceso. Revisa también la carpeta de spam.'


def normalize_email(value):
    value = value.strip()
    if len(value) > 254 or value.count('@') != 1:
        raise AgentError('invalid_email', 'Escribe un correo electrónico válido.', 422)
    local, domain = value.rsplit('@', 1)
    try:
        domain = domain.encode('idna').decode('ascii').lower()
    except UnicodeError:
        domain = ''
    if (not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", local)
            or local.startswith('.') or local.endswith('.') or '..' in local
            or len(domain) > 253 or '.' not in domain
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in domain.split('.'))):
        raise AgentError('invalid_email', 'Escribe un correo electrónico válido.', 422)
    return local.lower() + '@' + domain


class AccountMailer:
    def __init__(self, settings):
        self.settings = settings

    def public_url(self):
        value = self.settings.public_url.rstrip('/')
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(ord(char) < 33 for char in value)
                or parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}):
            raise AgentError('mail_not_configured', 'El envío de correos todavía no está configurado. Contacta con la administración.', 503)
        return value

    @property
    def configured(self):
        config = self.settings
        if not (config.smtp_host and config.smtp_username and config.smtp_password and config.smtp_security in {'ssl', 'starttls'} and 1 <= config.smtp_port <= 65535):
            return False
        try:
            normalize_email(config.smtp_from or config.smtp_username)
            self.public_url()
        except (AgentError, ValueError):
            return False
        return True

    def require_configured(self):
        if not self.configured:
            raise AgentError('mail_not_configured', 'El envío de correos todavía no está configurado. Tu cuenta se conserva; contacta con la administración.', 503)

    def send(self, recipient, subject, content):
        self.require_configured()
        config = self.settings
        message = EmailMessage()
        sender = normalize_email(config.smtp_from or config.smtp_username)
        message['From'] = sender
        message['To'] = normalize_email(recipient)
        message['Subject'] = subject
        message.set_content(content)
        context = ssl.create_default_context()
        try:
            if config.smtp_security == 'ssl':
                connection = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=15, context=context)
            else:
                connection = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15)
            with connection as client:
                if config.smtp_security == 'starttls':
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                password = config.smtp_password.replace(' ', '') if config.smtp_host == 'smtp.gmail.com' else config.smtp_password
                client.login(config.smtp_username, password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException):
            raise AgentError('mail_delivery_failed', 'No se pudo enviar el correo. Inténtalo más tarde o contacta con la administración.', 503) from None


class AccountRecovery:
    def __init__(self, database, settings):
        self.database = database
        self.mailer = AccountMailer(settings)

    def profile(self, user_id):
        with self.database.connection() as connection:
            row = connection.execute('SELECT email,verified FROM account_emails WHERE user_id=?', (user_id,)).fetchone()
        return {'email': row['email'], 'verified': bool(row['verified'])} if row else {'email': '', 'verified': False}

    def set_email(self, user_id, password, email):
        email = normalize_email(email)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            user = connection.execute('SELECT password FROM users WHERE id=?', (user_id,)).fetchone()
            if not user or not verify_password(password, user['password']):
                raise AgentError('invalid_credentials', 'La contraseña actual no es correcta.', 401)
            current = connection.execute('SELECT email FROM account_emails WHERE user_id=?', (user_id,)).fetchone()
            if current and current['email'] == email:
                return
            try:
                connection.execute('INSERT INTO account_emails(user_id,email,verified) VALUES(?,?,0) ON CONFLICT(user_id) DO UPDATE SET email=excluded.email,verified=0', (user_id, email))
            except sqlite3.IntegrityError:
                raise AgentError('email_unavailable', 'Ese correo ya está asociado a otra cuenta.', 409) from None
            connection.execute('DELETE FROM account_tokens WHERE user_id=?', (user_id,))

    def new_token(self, user_id, email, purpose):
        token = secrets.token_urlsafe(32)
        lifetime = 1800 if purpose == 'reset' else 86400
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM account_tokens WHERE user_id=? AND purpose=?', (user_id, purpose))
            connection.execute('INSERT INTO account_tokens(token_hash,user_id,purpose,email,expires_at) VALUES(?,?,?,?,?)', (token_hash(token), user_id, purpose, email, time.time() + lifetime))
        return token

    def deliver(self, user_id, email, username, purpose):
        self.mailer.require_configured()
        token = self.new_token(user_id, email, purpose)
        path = '/reset-password' if purpose == 'reset' else '/verify-email'
        link = self.mailer.public_url() + path + '#token=' + token
        subject = 'Recupera tu cuenta' if purpose == 'reset' else 'Confirma tu correo'
        action = 'restablecer tu contraseña' if purpose == 'reset' else 'confirmar el correo asociado a tu cuenta'
        lifetime = '30 minutos' if purpose == 'reset' else '24 horas'
        content = (f'Hola, {username}.\n\nUsa este enlace para {action}:\n\n{link}\n\n'
                   f'Este enlace caduca en {lifetime} y solo se puede utilizar una vez.\n'
                   'Si no solicitaste este mensaje, puedes ignorarlo.\n\nSistema Comunitario\n')
        try:
            self.mailer.send(email, subject + ' | Sistema Comunitario', content)
        except AgentError:
            with self.database.connection() as connection:
                connection.execute('DELETE FROM account_tokens WHERE token_hash=?', (token_hash(token),))
            raise

    def send_verification(self, user_id):
        self.mailer.require_configured()
        self.database.rate('verify-email:' + str(user_id), 3, 900)
        with self.database.connection() as connection:
            row = connection.execute('SELECT u.username,e.email,e.verified FROM users u JOIN account_emails e ON e.user_id=u.id WHERE u.id=?', (user_id,)).fetchone()
        if not row or row['verified']:
            raise AgentError('verification_unavailable', 'Afilia un correo pendiente de confirmar para continuar.', 422)
        self.deliver(user_id, row['email'], row['username'], 'verify')

    def send_reset(self, email):
        with self.database.connection() as connection:
            row = connection.execute('SELECT u.id,u.username,e.email FROM users u JOIN account_emails e ON e.user_id=u.id WHERE e.email=? AND e.verified=1', (email,)).fetchone()
        if row:
            try:
                self.deliver(row['id'], row['email'], row['username'], 'reset')
            except AgentError:
                logging.getLogger(__name__).warning('No se pudo entregar un correo de recuperación; revisa la configuración SMTP.')

    def consume(self, token, purpose, password=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise AgentError('invalid_account_token', INVALID_LINK, 400)
        if purpose == 'reset' and (password is None or not 8 <= len(password) <= 256):
            raise AgentError('weak_password', 'Usa entre 8 y 256 caracteres de contraseña.', 422)
        encoded = hash_password(password) if purpose == 'reset' else None
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT t.user_id,t.email FROM account_tokens t JOIN account_emails e ON e.user_id=t.user_id AND e.email=t.email WHERE t.token_hash=? AND t.purpose=? AND t.expires_at>? AND (t.purpose=\'verify\' OR e.verified=1)', (token_hash(token), purpose, time.time())).fetchone()
            if not row:
                raise AgentError('invalid_account_token', INVALID_LINK, 400)
            if purpose == 'reset':
                connection.execute('UPDATE users SET password=? WHERE id=?', (encoded, row['user_id']))
                connection.execute('DELETE FROM sessions WHERE user_id=?', (row['user_id'],))
                connection.execute('DELETE FROM account_tokens WHERE user_id=?', (row['user_id'],))
            else:
                connection.execute('UPDATE account_emails SET verified=1 WHERE user_id=?', (row['user_id'],))
                connection.execute('DELETE FROM account_tokens WHERE user_id=? AND purpose=\'verify\'', (row['user_id'],))
