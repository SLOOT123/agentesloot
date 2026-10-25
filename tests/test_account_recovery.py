"""HTTP y SQLite reales; entrega SMTP capturada, sin enviar correos externos."""
from concurrent.futures import ThreadPoolExecutor
import re
import smtplib
import ssl
import unittest
from unittest.mock import Mock, patch

import test_project as fixtures
from agent_core.account_recovery import AccountMailer, normalize_email
from agent_core.config import AgentError, Settings
from agent_core.security import COOKIE_NAME, verify_password


class AccountRecoveryTest(unittest.TestCase):
    tearDown = fixtures.ProjectTest.tearDown
    config = fixtures.ProjectTest.config
    save_config = fixtures.ProjectTest.save_config
    configure_model = fixtures.ProjectTest.configure_model
    register = fixtures.ProjectTest.register
    upstream = fixtures.ProjectTest.upstream

    def setUp(self):
        fixtures.ProjectTest.setUp(self)
        self.accounts = self.app.state.account_recovery
        self.settings.smtp_host = 'smtp.gmail.com'
        self.settings.smtp_username = 'sender@example.test'
        self.settings.smtp_password = 'fixture-only-smtp-secret'
        self.settings.public_url = 'https://community.example.test'
        self.messages = []
        self.delivery = patch.object(self.accounts.mailer, 'send', side_effect=lambda to, subject, content: self.messages.append({'to': to, 'subject': subject, 'content': content}))
        self.send = self.delivery.start()
        self.addCleanup(self.delivery.stop)

    def token(self, index=-1):
        return re.search(r'#token=([A-Za-z0-9_-]{43})', self.messages[index]['content'])[1]

    def confirm(self):
        token = self.token()
        response = self.client.post('/verify-email', data={'token': token})
        self.assertEqual(response.status_code, 200, response.text)
        return token

    def request_reset(self, email='alice@example.invalid'):
        response = self.client.post('/forgot-password', data={'email': email})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def reset(self, token, password='nueva-clave-456'):
        return self.client.post('/reset-password', data={'token': token, 'password': password, 'password_confirm': password}, follow_redirects=False)

    def test_registration_pages_link_email_and_confirm_the_mailbox_before_recovery(self):
        login = self.client.get('/login')
        self.assertIn('href="/register"', login.text)
        self.assertIn('href="/forgot-password"', login.text)
        page = self.client.get('/register')
        self.assertIn('name="email"', page.text)
        self.assertIn('name="password_confirm"', page.text)
        self.assertEqual(page.headers['referrer-policy'], 'same-origin')
        self.register()
        self.assertEqual(self.accounts.profile(1), {'email': 'alice@example.invalid', 'verified': False})
        self.assertEqual(self.messages[0]['to'], 'alice@example.invalid')
        self.assertIn('https://community.example.test/verify-email#token=', self.messages[0]['content'])
        token = self.token()
        self.assertNotIn(token, self.client.get('/verify-email').text)
        self.assertFalse(self.accounts.profile(1)['verified'], 'Abrir la página no debe consumir la confirmación')
        self.confirm()
        self.assertTrue(self.accounts.profile(1)['verified'])
        self.assertEqual(self.client.post('/verify-email', data={'token': token}).status_code, 400)

    def test_registration_rejects_missing_duplicate_email_and_confirmation_mismatch_atomically(self):
        data = {'username': 'alice', 'password': 'prueba-segura-123', 'password_confirm': 'prueba-segura-123', 'action': 'register'}
        self.assertEqual(self.client.post('/auth', data=data).status_code, 422)
        self.assertEqual(self.client.post('/auth', data={**data, 'email': 'alice@example.invalid', 'password_confirm': 'otra'}).status_code, 422)
        self.register()
        response = self.client.post('/auth', data={**data, 'username': 'bob', 'email': 'ALICE@EXAMPLE.INVALID'})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('prueba-segura-123', response.text)
        with self.app.state.database.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)

    def test_native_registration_posts_work_from_both_local_addresses(self):
        for index, origin in enumerate(('http://localhost:8000', 'http://127.0.0.1:8000')):
            with self.subTest(origin=origin):
                page = self.client.get(origin + '/register')
                self.assertEqual(page.headers['referrer-policy'], 'same-origin')
                self.assertIn('name="referrer" content="same-origin"', page.text)
                response = self.client.post(origin + '/auth', headers={
                    'Origin': origin, 'Referer': origin + '/register', 'Sec-Fetch-Site': 'same-origin',
                    'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document', 'Accept': 'text/html'},
                    data={'action': 'register', 'username': 'local' + str(index), 'email': 'local' + str(index) + '@example.invalid',
                          'password': 'prueba-segura-123', 'password_confirm': 'prueba-segura-123'}, follow_redirects=False)
                self.assertEqual(response.status_code, 303, response.text)
                account = self.client.get(origin + response.headers['location'])
                self.assertEqual(account.status_code, 200)
                self.assertIn('Mi cuenta', account.text)

    def test_account_forms_preserve_origin_and_still_reject_null_or_external_origins(self):
        self.register()
        for path in ('/register', '/account', '/forgot-password', '/reset-password', '/verify-email'):
            with self.subTest(path=path):
                page = self.client.get(path)
                self.assertEqual(page.headers['referrer-policy'], 'same-origin')
                self.assertNotIn('name="referrer" content="no-referrer"', page.text)
        with self.app.state.database.connection() as connection:
            count = connection.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        for origin, site in (('null', 'same-origin'), ('https://evil.example', 'same-origin'), ('http://testserver', 'cross-site')):
            with self.subTest(origin=origin, site=site):
                response = self.client.post('/auth', headers={'Origin': origin, 'Sec-Fetch-Site': site}, data={
                    'action': 'register', 'username': 'blocked', 'email': 'blocked@example.invalid',
                    'password': 'prueba-segura-123', 'password_confirm': 'prueba-segura-123'})
                self.assertEqual(response.status_code, 403)
        with self.app.state.database.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM users').fetchone()[0], count)

    def test_known_unknown_and_unverified_addresses_get_the_same_public_response(self):
        self.register()
        unverified = self.request_reset()
        self.assertEqual(len(self.messages), 1)
        self.confirm()
        known = self.request_reset()
        unknown = self.request_reset('nobody@example.invalid')
        self.assertEqual(unverified.text, known.text)
        self.assertEqual(known.text, unknown.text)
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(len(self.messages), 2)
        self.assertNotIn(self.token(), known.text)
        self.assertNotIn('alice', unknown.text)

    def test_reset_is_single_use_updates_password_and_revokes_all_sessions(self):
        self.register()
        first_session = self.client.cookies.get(COOKIE_NAME)
        self.confirm()
        self.client.post('/auth', data={'username': 'alice', 'password': 'prueba-segura-123'})
        second_session = self.client.cookies.get(COOKIE_NAME)
        self.request_reset()
        token = self.token()
        with self.app.state.database.connection() as connection:
            stored = connection.execute("SELECT token_hash FROM account_tokens WHERE purpose='reset'").fetchone()[0]
        self.assertNotEqual(stored, token)
        response = self.reset(token)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/login?reset=1')
        self.assertIsNone(self.app.state.database.session(first_session))
        self.assertIsNone(self.app.state.database.session(second_session))
        self.assertEqual(self.reset(token).status_code, 400)
        self.assertEqual(self.client.post('/auth', data={'username': 'alice', 'password': 'prueba-segura-123'}).status_code, 401)
        self.assertEqual(self.client.post('/auth', data={'username': 'alice', 'password': 'nueva-clave-456'}, follow_redirects=False).status_code, 303)
        self.assertIn('Contraseña actualizada', self.client.get('/login?reset=1').text)

    def test_expired_replaced_and_wrong_purpose_tokens_cannot_change_password(self):
        self.register()
        verify_token = self.token()
        self.assertEqual(self.reset(verify_token).status_code, 400)
        self.confirm()
        self.request_reset()
        replaced = self.token()
        self.request_reset()
        current = self.token()
        self.assertNotEqual(current, replaced)
        self.assertEqual(self.reset(replaced).status_code, 400)
        self.assertEqual(self.client.post('/verify-email', data={'token': current}).status_code, 400)
        with self.app.state.database.connection() as connection:
            connection.execute('UPDATE account_tokens SET expires_at=0')
        self.assertEqual(self.reset(current).status_code, 400)
        self.assertEqual(self.client.post('/auth', data={'username': 'alice', 'password': 'prueba-segura-123'}, follow_redirects=False).status_code, 303)

    def test_bad_password_confirmation_does_not_consume_reset_link(self):
        self.register(); self.confirm(); self.request_reset()
        token = self.token()
        response = self.client.post('/reset-password', data={'token': token, 'password': 'nueva-clave-456', 'password_confirm': 'diferente'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.reset(token, 'short').status_code, 422)
        self.assertEqual(self.reset(token).status_code, 303)

    def test_existing_accounts_can_affiliate_email_with_password_and_csrf(self):
        database = self.app.state.database
        database.authenticate('legacy', 'clave-original', True, 'fixture')
        self.client.post('/auth', data={'username': 'legacy', 'password': 'clave-original'})
        csrf = self.client.get('/api/session').json()['csrf_token']
        self.assertIn('Todavía no afiliaste', self.client.get('/account').text)
        data = {'email': 'legacy@example.invalid', 'current_password': 'clave-original', 'csrf_token': csrf}
        self.assertEqual(self.client.post('/account/email', data={**data, 'csrf_token': 'wrong'}).status_code, 403)
        self.assertEqual(self.client.post('/account/email', data={**data, 'current_password': 'wrong'}).status_code, 401)
        self.assertEqual(self.accounts.profile(1)['email'], '')
        self.assertEqual(self.client.post('/account/email', data=data).status_code, 200)
        self.assertEqual(self.accounts.profile(1)['email'], 'legacy@example.invalid')
        self.confirm()
        self.assertTrue(self.accounts.profile(1)['verified'])

    def test_changing_email_invalidates_previous_links_and_requires_new_confirmation(self):
        headers = self.register(); self.confirm(); self.request_reset()
        token = self.token()
        response = self.client.post('/account/email', data={'email': 'new@example.invalid', 'current_password': 'prueba-segura-123', 'csrf_token': headers['X-CSRF-Token']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.accounts.profile(1)['verified'])
        self.assertEqual(self.reset(token).status_code, 400)
        count = len(self.messages)
        self.request_reset('new@example.invalid')
        self.assertEqual(len(self.messages), count)
        self.confirm()
        self.request_reset('new@example.invalid')
        self.assertEqual(len(self.messages), count + 1)

    def test_missing_mail_configuration_is_reported_without_losing_the_account(self):
        self.settings.smtp_password = ''
        self.register()
        self.assertEqual(len(self.messages), 0)
        self.assertIn('aún no está habilitado', self.client.get('/account').text)
        response = self.client.post('/forgot-password', data={'email': 'alice@example.invalid'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('todavía no está configurado', response.text)
        self.assertNotIn('Revisa tu correo', response.text)
        self.assertEqual(self.client.post('/auth', data={'username': 'alice', 'password': 'prueba-segura-123'}, follow_redirects=False).status_code, 303)

    def test_failed_mail_delivery_does_not_leave_an_active_token_or_disclose_details(self):
        self.register(); self.confirm()
        self.send.side_effect = AgentError('mail_delivery_failed', 'fixture-only-smtp-secret', 503)
        with self.assertLogs('agent_core.account_recovery', level='WARNING') as captured:
            response = self.request_reset()
        self.assertNotIn('fixture-only-smtp-secret', response.text + ''.join(captured.output))
        with self.app.state.database.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM account_tokens').fetchone()[0], 0)

    def test_recovery_rate_limit_and_cross_site_requests(self):
        self.assertEqual(self.client.post('/forgot-password', data={'email': 'a@example.invalid'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        for _ in range(3):
            self.request_reset('unknown@example.invalid')
        self.assertEqual(self.client.post('/forgot-password', data={'email': 'unknown@example.invalid'}).status_code, 429)

    def test_two_concurrent_uses_of_the_same_link_only_reset_once(self):
        self.register(); self.confirm(); self.request_reset()
        token = self.token()
        def use():
            try:
                self.accounts.consume(token, 'reset', 'nueva-clave-456')
                return 'success'
            except AgentError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: use(), range(2)))
        self.assertCountEqual(results, ['success', 'invalid_account_token'])


class MailerTest(unittest.TestCase):
    def settings(self):
        from pathlib import Path
        return Settings(Path('.'), Path('unused.db'), smtp_host='smtp.gmail.com', smtp_username='sender@example.test', smtp_password='abcd efgh ijkl mnop')

    def test_email_normalization_and_header_injection_rejection(self):
        self.assertEqual(normalize_email(' ALICE@Example.org '), 'alice@example.org')
        for value in ('a@example.org\r\nBcc: other@example.org', '.a@example.org', 'a..b@example.org', 'a@localhost', 'a@-bad.example', 'a@bad..example'):
            with self.subTest(value=value), self.assertRaises(AgentError):
                normalize_email(value)

    def test_gmail_uses_verified_tls_before_login_and_keeps_secrets_out_of_repr(self):
        config = self.settings()
        smtp = Mock()
        smtp.__enter__ = Mock(return_value=smtp)
        smtp.__exit__ = Mock(return_value=False)
        with patch('agent_core.account_recovery.smtplib.SMTP', return_value=smtp) as connect:
            AccountMailer(config).send('user@example.test', 'Prueba', 'Contenido de prueba')
        self.assertEqual(connect.call_args.args, ('smtp.gmail.com', 587))
        self.assertEqual([call[0] for call in smtp.method_calls], ['ehlo', 'starttls', 'ehlo', 'login', 'send_message'])
        context = smtp.starttls.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        smtp.login.assert_called_once_with('sender@example.test', 'abcdefghijklmnop')
        self.assertEqual(smtp.send_message.call_args.args[0]['To'], 'user@example.test')
        self.assertNotIn('abcd efgh', repr(config))

    def test_ssl_transport_and_smtp_failure_have_safe_errors(self):
        config = self.settings(); config.smtp_security = 'ssl'; config.smtp_port = 465
        smtp = Mock(); smtp.__enter__ = Mock(return_value=smtp); smtp.__exit__ = Mock(return_value=False)
        with patch('agent_core.account_recovery.smtplib.SMTP_SSL', return_value=smtp) as connect:
            AccountMailer(config).send('user@example.test', 'Prueba', 'Contenido')
        self.assertEqual(connect.call_args.args, ('smtp.gmail.com', 465))
        smtp.starttls.assert_not_called()
        with patch('agent_core.account_recovery.smtplib.SMTP_SSL', side_effect=smtplib.SMTPAuthenticationError(535, b'private smtp details')):
            with self.assertRaises(AgentError) as captured:
                AccountMailer(config).send('user@example.test', 'Prueba', 'Contenido')
        self.assertEqual(captured.exception.code, 'mail_delivery_failed')
        self.assertNotIn('private smtp', captured.exception.message)

    def test_reset_links_require_a_configured_origin_and_never_use_request_host(self):
        config = self.settings()
        for value in ('http://untrusted.example', 'https://user:pass@example.org', 'https://example.org/#evil', 'https://example.org/?evil', 'javascript:alert(1)'):
            config.public_url = value
            self.assertFalse(AccountMailer(config).configured)


if __name__ == '__main__':
    unittest.main()
