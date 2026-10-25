# Correo y recuperación de cuenta

En el acceso, **Crear cuenta** abre un formulario con alias, correo y contraseña repetida. **Olvidé mi contraseña · Recuperar cuenta** permite pedir un enlace al correo afiliado y confirmado. El mensaje recibido también indica el alias de la cuenta. El enlace de recuperación dura 30 minutos y admite un solo uso. Al guardar la nueva contraseña se cierran todas las sesiones de esa cuenta y se vuelve a iniciar sesión.

Las cuentas anteriores conservan sus contraseñas, chats y archivos. Para afiliar un correo, inicia sesión y abre **Mi cuenta · Correo de recuperación** en la barra lateral. Escribe la dirección y tu contraseña actual; después confirma el enlace recibido. Cambiar de dirección invalida los enlaces anteriores y requiere confirmar el nuevo correo. El enlace de confirmación dura 24 horas. Desde Mi cuenta puedes reenviar la confirmación.

## Activar Gmail

Completa estas variables en el archivo privado `.env` del proyecto:

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=tu-remitente@gmail.com
SMTP_PASSWORD=tu-contraseña-de-aplicación
SMTP_FROM=
APP_PUBLIC_URL=http://127.0.0.1:8000
```

`SMTP_USERNAME` es la cuenta que enviará los mensajes a los usuarios. `SMTP_FROM` puede quedar vacío para usar esa misma dirección. `SMTP_PASSWORD` es una **contraseña de aplicación de Google**, que se genera después de activar la verificación en dos pasos; no uses la contraseña habitual ni la compartas por el chat. Google explica los requisitos y las restricciones de algunas cuentas en [Contraseñas de aplicaciones](https://support.google.com/accounts/answer/185833?hl=es). El servidor usa STARTTLS con comprobación del certificado antes de autenticar. Los parámetros del servicio están en la [documentación oficial de Gmail SMTP](https://developers.google.com/workspace/gmail/imap/imap-smtp).

`APP_PUBLIC_URL` debe ser la dirección real de la aplicación. El valor local solo permite abrir los enlaces desde este equipo. Para abrirlos desde otro equipo o teléfono, configura la URL HTTPS accesible a esos usuarios. Los enlaces no se construyen a partir del encabezado Host enviado por el navegador. Después de completar `.env`, reinicia el servidor y solicita una confirmación desde Mi cuenta. No hace falta instalar paquetes adicionales.

Si faltan los datos de Gmail, puedes crear cuentas y guardar correos, pero la interfaz indica que el envío está pendiente de habilitar. No se anuncia un correo enviado cuando el servicio no está configurado. Las respuestas públicas de recuperación son iguales para direcciones registradas y no registradas.

## Comprobaciones

`tests/test_account_recovery.py` usa rutas HTTP, sesiones y SQLite reales, con entrega de correo capturada. Comprueba confirmación, recuperación completa, expiración, reutilización y propósito de tokens, cambio de correo, CSRF, límites de solicitudes, uso concurrente, conservación de cuentas antiguas, cierre de sesiones y errores de SMTP. Los tokens se guardan como SHA-256 y los enlaces los llevan en el fragmento `#token=...`, para evitar incluirlos en los registros HTTP. El navegador elimina ese fragmento al cargar el formulario. La contraseña se almacena con scrypt.

El diseño sigue las medidas de [OWASP para recuperar contraseñas](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html). Estas pruebas no envían mensajes a Gmail; la entrega real debe comprobarse después de completar las credenciales privadas.
