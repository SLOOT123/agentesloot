# Tu comunidad y tu agente

Se conserva el proyecto FastAPI, el canal comunitario, SQLite, el login, el logo, el video y los efectos existentes. El agente se añadió como un modo privado separado.

El **canal general principal** también responde con IA a cada mensaje nuevo usando el proveedor predeterminado. Los mensajes y las respuestas son compartidos; su contexto contiene únicamente el historial público. Las investigaciones usan el equipo de búsqueda y conservan sus fuentes para todos los usuarios. El estado de cada respuesta aparece junto al mensaje mientras trabaja o si falla. Las conversaciones, documentos y herramientas privados se usan desde **Mi agente · privado**. Las respuestas pendientes se conservan al reiniciar y los mensajes antiguos no generan respuestas retroactivas.

**Ya está conectado Ollama local con `llama3.2:3b`.** Se comprobaron respuestas reales, memoria documental, creación y ejecución de un proyecto en Docker e investigación con fuentes. No se inició entrenamiento. Consulta [cómo iniciar y probar la conexión local](docs/CONEXION_LOCAL.md).

**Equipo multiagente activado:** el perfil predeterminado es **Equipo · investigar, ejecutar y verificar**. Dos investigadores pueden buscar y leer en paralelo; el servidor contrasta sus extractos y valida las citas antes de entregar la síntesis. Para programación y matemáticas incorpora especialistas de requisitos y verificación, ejecución y revisión independiente, con hasta una reparación del equipo. Usa trabajos persistentes en segundo plano: puedes cambiar de conversación y volver a consultar el resultado. Consulta [arquitectura, límites y recuperación del equipo](docs/ARQUITECTURA_MULTIAGENTE.md).

**Perfil Luis:** en **Mi agente · privado → Perfil** selecciona **Luis · ejecutar y verificar**. Incorpora la voz directa y formal del texto proporcionado, con encabezado LUISTON, presentación breve, título y entrega. Comparte las herramientas del equipo y conserva las fuentes y comprobaciones; el formato se añade al resultado final para respetar los contratos JSON de las herramientas. Sus instrucciones editables están en `prompts/luis.md` y sus capacidades en `profiles/luis.json`. Recarga con `Ctrl+F5` para actualizar la lista. Es un perfil adicional; las conversaciones existentes conservan su perfil.

La adaptación es una configuración de comportamiento; mantiene los permisos y la verificación existentes. Para comprobarla con el modelo real y SQLite temporal, ejecuta `scripts/check_luis.py` para el saludo y `scripts/check_team.py --profile luis --code` para el proyecto y sus pruebas, usando el Python de `.venv`.

La configuración actual permite dos trabajos privados globales, uno por usuario, y una llamada simultánea al modelo compartida por los roles de la aplicación y el principal. El equipo dispone de 600 segundos de ejecución. Una investigación sin evidencia suficiente queda parcial o bloqueada; los roles adicionales no garantizan más velocidad ni exactitud universal. Son procesos coordinados en este equipo, sin desplegar otras máquinas virtuales.

**Agente con grafos:** el perfil **Agente · planificar y ejecutar** registra dependencias, ejecuta herramientas y cierra las acciones al terminar el plan. La búsqueda incorpora clasificación BM25 y el grafo de fuentes conserva fracciones exactas. Se añadió una base de evaluación offline antes de entrenar; no se modificaron pesos. Consulta [uso, matemáticas y evaluación](docs/AGENTE_GRAFOS.md) y la [revisión del 17 de octubre](docs/REVISION_2026-10-17.md).

## Ejecutar en Windows / PowerShell

Desde esta carpeta, sin necesidad de activar el entorno:

```powershell
Set-Location 'C:\Users\sloty\Desktop\fash toch'
.\.venv\Scripts\python.exe -X utf8 -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 app.py
```

Abre <http://127.0.0.1:8000>. Para detener ese servidor, pulsa `Ctrl+C` en su terminal. Si ejecutas otra vez `python app.py` y la misma instancia ya está funcionando, el comando muestra su enlace y termina sin iniciar una segunda copia. La comprobación usa `/healthz` y distingue este proyecto y base de datos de otro servicio que ocupe el puerto. Si el puerto pertenece a otro proceso, lo informa sin detenerlo. Para aplicar cambios del backend, detén la instancia actual y vuelve a arrancarla; repetir el comando no reemplaza una instancia activa.

En el login:

- Cuenta existente: introduce su contraseña real y pulsa **Entrar al sistema**.
- Cuenta nueva: pulsa **Crear cuenta**, elige un alias, un correo y una contraseña de al menos ocho caracteres. Confirma la contraseña y después el correo recibido.
- **Olvidé mi contraseña · Recuperar cuenta** envía un enlace de un uso al correo confirmado. Las cuentas existentes pueden afiliar su correo desde **Mi cuenta · Correo de recuperación**.
- Las cookies antiguas dejaron de servir para entrar. Es necesario iniciar sesión otra vez.

La primera ejecución migra las contraseñas existentes a hashes scrypt. Hace una copia previa de SQLite en `.local/backups/` y conserva los alias y mensajes. La copia contiene la versión anterior de los datos: mantenla privada y fuera de carpetas públicas.

El envío de confirmaciones y recuperación necesita configurar Gmail en `.env`. El formulario informa si aún no está habilitado y las cuentas siguen pudiendo iniciar sesión. Consulta [la configuración y el flujo de recuperación](docs/RECUPERAR_CUENTA.md).

## Elegir el proveedor cuando estés listo

Edita `config/agent.json`, sin modificar el núcleo del agente.

**Ollama:** inicia tu instalación de Ollama, consulta `ollama list` y copia un nombre exacto en `backends.local.model`. La URL nativa es `http://127.0.0.1:11434`; no añadas `/v1` a este adaptador. No se descargan modelos automáticamente.

**API externa:** copia `.env.example` a `.env` si todavía no existe y añade tu clave a `AGENT_API_KEY`. En `backends.externo`, configura `base_url` y `model` según el proveedor. El adaptador implementa **Chat Completions compatible**; las APIs con otro protocolo necesitan otro adaptador. No todas las APIs que se anuncian como compatibles admiten las mismas opciones.

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Reinicia el servidor después de cambiar `.env`. No pegues claves en el navegador, los prompts ni el chat. El selector de la interfaz permite elegir un proveedor configurado. `default_backend` determina la opción inicial.

En **Mi agente · privado**, elige perfil/proveedor y pulsa **Comprobar modelo**. Para comprobarlo desde terminal:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agent_core.check --backend local
```

Una comprobación de API externa realiza una pequeña consulta real que puede consumir cuota. `verify_tools: true` añade otra prueba para comprobar llamadas de herramientas. Ollama informa sus capacidades mediante `/api/show`; el diagnóstico distingue capacidades declaradas, verificadas y no comprobadas. El contexto externo es una configuración del servidor, no una medición automática de su tokenizer.

## Personalizar y usar

- Instrucciones comunes: `prompts/base.md`.
- Tus instrucciones por perfil: `prompts/general.md` y `prompts/investigador.md`.
- Nombre, descripción, memoria y herramientas permitidas: `profiles/*.json`.
- Contratos de propósito y criterios de aceptación: `agent_skills/*.json`; el equipo valida su identidad y versión.
- **Canal general:** comparte mensajes y respuestas con la comunidad y consulta novedades cada tres segundos. Los saludos usan texto; la investigación usa dos especialistas y fuentes del contexto público.
- **Mi agente · privado:** conserva un historial por conversación y por usuario. Cambiar perfil/proveedor o pulsar Nuevo chat en este modo crea otra conversación. Los trabajos continúan en el servidor; al volver recupera su estado. **Cancelar trabajo** detiene una tarea pendiente.
- **Equipo · investigar, ejecutar y verificar:** opción inicial para conversaciones nuevas. Muestra especialistas, lecturas, ejecuciones y revisión en **Trabajo en segundo plano → Ampliar monitor** y **Registro de actividad**. Conserva informes por fases y snapshots de proyectos; puede reutilizar fases completas y válidas al recuperar un trabajo tras reiniciar.
- **Perfil Mi agente:** ofrece memoria, calculadora y búsqueda web cuando el modelo la solicita. La búsqueda necesita SearXNG; la lectura de sus enlaces requiere además Docker. Los saludos no fuerzan una búsqueda.
- **Mi memoria documental:** guarda textos y recupera fragmentos por coincidencia de términos. Los PDF, imágenes y documentos se suben desde **Archivos del chat**, dentro de cada conversación. Los fragmentos pertinentes se envían al modelo elegido; con una API externa, salen del equipo. Guardarlos no entrena el modelo.
- **Investigador:** requiere `SEARXNG_URL` en `.env`, apuntando a una instancia de SearXNG que permita respuestas JSON. Con el equipo activado distribuye la investigación entre dos enfoques. `web_search` devuelve fragmentos para localizar páginas; solo extractos obtenidos mediante `web_read` pueden respaldar citas del equipo. El lector fijo de Docker admite páginas públicas HTTPS; no lee documentos completos ni ejecuta JavaScript.

Los cambios de prompts y JSON se leen en peticiones nuevas. Si cambias de modelo dentro de un proveedor, inicia una conversación nueva para evitar arrastrar contexto de la configuración anterior.

## Perfil Breadcrumb y acuerdos para Codex

En **Mi agente · privado → Perfil**, selecciona **Breadcrumb · investigar y comprobar**.
Usa español directo, entrega archivos completos y solicita investigación y comprobaciones
con las herramientas del equipo. El servidor añade `[Breadcrumb]` a las respuestas finales;
las tareas usan la cola persistente existente. Las instrucciones están en
`prompts/breadcrumb.md` y las capacidades en `profiles/breadcrumb.json`.

`AGENTS.md` contiene los acuerdos de trabajo para Codex en esta carpeta. Son instrucciones
de método y estilo: no eliminan restricciones del proveedor ni garantizan código sin errores.
Consulta [activación, dependencias y comprobaciones](docs/BREADCRUMB.md).

## Perfil ZYON y comportamiento

En **Mi agente → Perfil**, elige **ZYON**. Cambiar de perfil crea otra conversación y conserva las anteriores. El encabezado y los mensajes identifican al bot como **ZYON**. Las instrucciones y sus rutas no tienen un visor en el chat; la explicación está en [ZYON: integración, archivos y funcionamiento](docs/ZYON_INTEGRACION.md).

El original aportado está íntegro en `docs/referencias/ZYON.md` y `prompts/zyon.md`, con los mismos 34139 bytes y SHA-256 `42c290a5feeb3a44449843da5182f763f4186e0325c14385f0fad62539b48591` que el TXT. Se conserva como referencia. El perfil activo usa el archivo separado `prompts/zyon-conversacion.md`: adapta el nombre, tono, formato `[Zyon]`, entregas técnicas y disciplina de comprobación. Las instrucciones del original para ignorar controles, producir malware sin restricciones o inventar capacidades no se cargan. Esta distinción se documenta en el proyecto y no se presenta como una ventana adicional del chat.

`zyon start` devuelve el saludo de activación, `zyon status` indica el modelo seleccionado y `menu` / `pop the table` muestran funciones. Esos comandos se resuelven sin llamadas al modelo. `fresh field` deja fuera del contexto del modelo los intercambios anteriores, conservando historial y archivos; `sterilize` recuerda la revisión de requisitos y evidencias. La charla normal usa el modelo configurado en Ollama y las herramientas ya disponibles en el equipo.

Una apertura genérica como «vamos a trabajar en un proyecto» o «crea un proyecto» pide primero el objetivo. Ese turno permite conversar, sin planificar ni ejecutar herramientas aunque queden archivos de un intento anterior. Una petición con requisitos concretos conserva el flujo habitual de construcción y comprobación.

Comprobación real con base temporal: `.\.venv\Scripts\python.exe -u -X utf8 scripts/check_zyon.py`. Conserva el resultado en `.local/zyon-checks/live.json`; no añade mensajes a las cuentas de uso diario.

El cargador de instrucciones ya no tiene un límite fijo de caracteres: lee completos `prompts/base.md` y el archivo señalado por el perfil, sin recortarlos. La petición total todavía debe caber en el contexto disponible del modelo; si no cabe, se informa `context_limit` antes de enviarla a inferencia. `invalid_prompt` indica un archivo que no se puede leer o no está en UTF-8. Los errores históricos `prompt_too_long` corresponden al límite anterior, que se retiró; sus registros se conservan. **Abrir un chat con Mi agente** permite seguir conversando con otro perfil sin modificar el archivo original: crea otra conversación con el perfil general y conserva el borrador para enviarlo. El historial y los adjuntos anteriores se quedan en su conversación.

Tras reparar el archivo, recarga el chat y reenvía tu mensaje. El monitor comprueba si las instrucciones actuales ya se pueden cargar e identifica el error como un intento anterior; conserva ese intento como fallido y no envía mensajes automáticamente.

## Subir documentos e imágenes

En **Mi agente**, usa **Archivos del chat → Subir archivos** en el costado o el botón **＋** junto al mensaje. Puedes elegir varios archivos, arrastrarlos al panel o pegar una imagen desde el portapapeles. Después pregunta por su contenido: «Resume este PDF», «¿Qué error muestra la foto?» o «Busca esta frase en el Markdown». No hace falta pegar el documento completo dentro del mensaje.

Admite TXT/Markdown y otros archivos de texto o código, CSV, JSON, HTML/XML, PDF, DOCX, XLSX, PPTX e imágenes PNG/JPG/WebP/GIF/BMP/TIFF/AVIF. El texto se extrae localmente y se consulta por fragmentos. Las fotos y páginas escaneadas usan el especialista local **Qwen3-VL 2B**, separado del modelo de chat. Los archivos, vistas previas y referencias pertenecen únicamente a su conversación y propietario.

Límites actuales: 20 MB por archivo, 20 archivos y 100 MB por conversación; extracción de hasta 200000 caracteres y 50 páginas de PDF; análisis visual de hasta tres vistas por mensaje. Un GIF/TIFF animado usa su primer fotograma. Si un formato no tiene lector o el archivo está dañado, se informa el motivo. Los detalles y comprobaciones están en [archivos adjuntos y visión local](docs/ARCHIVOS_ADJUNTOS.md).

## Ver el trabajo en segundo plano

La **Consola de ejecución** aparece abierta dentro de **Trabajo en segundo plano**, en la misma pantalla del chat. Muestra comandos, salida real de herramientas y contenedores, progreso y errores sin abrir una terminal externa. **Seguir salida** controla el desplazamiento y **Ocultar** pliega la consola sin detener los trabajos. Las descargas reconocidas usan primero su extractor específico. La recuperación persistente está activada: los fallos temporales siguen reintentándose con espera hasta completar o cancelar. Un requisito externo conserva el objetivo como **blocked**, con diagnóstico y acción para reanudar. `tool_discover` permite al modelo consultar las herramientas permitidas y su disponibilidad antes de planificar o cambiar de estrategia.

El [motor matemático y sus comandos de observación](docs/MOTOR_MATEMATICO.md) combina BM25, vectores TF-IDF y coseno para la búsqueda, y vectores para la memoria privada. La descarga registra PID, comando y cierre del trabajador; la espera por HTTP 429 muestra el próximo intento y una cuenta regresiva. `scripts/monitor_agent.py --watch` consulta los estados desde terminal y `scripts/check_math_pipeline.py` ejecuta los módulos matemáticos reales en Docker, sin red ni llamadas a la IA. La evaluación conserva BM25 como comparación; todavía no demuestra una mejora general del ranking híbrido.

Dentro de **Mi agente**, envía una petición y pulsa **Trabajo en segundo plano → Ampliar monitor**. La vista muestra el estado, los especialistas, los pasos observados del plan, las descargas y las últimas acciones. Puedes cerrarla y seguir escribiendo. El registro técnico queda plegado debajo para consultarlo cuando lo necesites.

Para ver una ejecución de ejemplo, pide: «Crea un archivo Python que calcule 2 + 2, ejecútalo y comprueba el resultado». Cuando el agente solicite Docker, verás el nombre asignado, los límites configurados, las muestras de consola y el resultado final. Python envía la salida sin esperar a que termine el script; el monitor consulta novedades aproximadamente cada segundo. Solo muestra contenedores de ese trabajo y conserva hasta ocho en la vista. No son mediciones de consumo de CPU/RAM. Una interrupción sin evento de cierre se muestra como **Sin cierre registrado**.

El código generado se ejecuta en Docker sin red. El lector fijo de páginas usa otro contenedor con red; el modelo Ollama y las descargas funcionan como procesos locales separados. Una descarga puede continuar después de que termine el turno del chat. Solo la transferencia con datos medidos muestra porcentaje; la conversión indica su fase. Los archivos listos se guardan desde **Archivos y descargas**.

## Descargar desde enlaces

En **Mi agente** o **Agente · planificar y ejecutar**, envía «Descarga el audio en MP3 de este enlace: https://…». El enlace del usuario se puede usar directamente; no necesita pasar primero por una búsqueda. El archivo comprobado aparece en **Archivos y descargas**, con un botón para guardarlo desde el navegador.

Las órdenes de descarga con un enlace se programan directamente, sin pedir el título. En **Archivos y descargas** también puedes pegar una URL y seleccionar **Vídeo completo**, **Solo audio · MP3** o **Archivo original**. El chat responde enseguida y la descarga continúa en segundo plano, con progreso, cancelación y reintento. Cuando imagen y sonido vienen separados, FFmpeg los une y se comprueban ambas pistas. Los originales completos se conservan si falla la preparación; los vídeos largos no quedan sujetos al tiempo del chat ni a un corte de conversión de 35 segundos.

También puedes pedir «Descarga el PDF de esta página: https://…» o elegir **PDF de la página**. El descargador encuentra enlaces, botones y documentos incrustados, abre el PDF para comprobar sus páginas y conserva formatos DOCX/XLSX/PPTX y OpenDocument reconocidos. **Ver herramientas y pasos** abre una ventana dentro del chat con las operaciones, los comandos multimedia y los fallos/reintentos; se actualiza en segundo plano y conserva los pasos al reanudar. Si un sitio exige pago, login o verificación, necesita un enlace de descarga habilitado con acceso autorizado; no toma automáticamente la sesión del navegador ni rodea sus bloqueos.

`download_tools` detecta HTTP, yt-dlp, FFmpeg, ffprobe y Node. `download_url` prueba archivos directos, enlaces multimedia de páginas y yt-dlp con reintentos. FFmpeg convierte el audio y ffprobe comprueba las pistas. FFmpeg/ffprobe y Node ya están instalados en este equipo; yt-dlp se incluyó en `requirements.txt`. Se conserva un solo vídeo cuando el enlace también incluye una lista de radio. Un bloqueo del sitio se informa como pendiente, sin presentar una descarga falsa. [Guía y pruebas de descargas](docs/DESCARGAS.md).

## Verificar

Ejecuta estos comandos desde la carpeta del proyecto. Para la comprobación HTTP, abre otra terminal y mantén el servidor iniciado en la primera.

```powershell
# Rutas, autenticación, SQLite, aislamiento y adaptadores (sin pagar un modelo).
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v

# Módulos JS reales con DOM y HTTP simulados; no sustituye un navegador.
node tests/test_frontend.cjs
node tests/test_attachments_frontend.cjs

# Con el servidor iniciado: HTTP real, logo, video y rechazo de accesos anónimos.
.\.venv\Scripts\python.exe -X utf8 scripts/check_http.py

# Principal con modelo real y SQLite temporal; no publica pruebas en el canal de uso diario.
.\.venv\Scripts\python.exe -X utf8 scripts/check_community.py

# Equipo real con SQLite temporal: búsqueda, lectura y revisión de fuentes.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_team.py

# Equipo real: crea archivos y pruebas, ejecuta la versión guardada en Docker y la revisa.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_team.py --code

# Archivos reales y modelos locales con SQLite temporal: MD largo, PDF, foto y PDF escaneado.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_attachments.py
```

Las pruebas del equipo guardan informes en `.local/team-checks/` y fallan si el resultado queda incompleto. Usan el proveedor configurado y pueden consumir cuota si seleccionas una API externa. La recuperación revalida fases conservadas; no mantiene un contenedor o script suspendido durante el reinicio.

En una comprobación manual usa dos perfiles de navegador o una ventana privada: ambos usuarios deben ver el canal general, pero no las conversaciones ni documentos privados del otro. Guarda un documento de texto, pregunta por su contenido después de configurar un modelo y abre otra conversación para verificar que no hereda el historial anterior.

Consulta [la revisión del proyecto](docs/REVISION.md), [la guía de arquitectura, límites y archivos](docs/AGENTE.md) y [cómo mejorar el agente con LangChain, LangGraph y modelos propios](docs/POTENCIA.md).

## Laboratorio: ejecutar, corregir y recordar errores

El perfil **Laboratorio · corregir y comprobar** crea scripts Python o comandos `sh`, los ejecuta en Docker y recibe sus errores para intentar corregirlos. El **Registro de actividad**, dentro del mismo chat, muestra código, salida, errores, revisión y relaciones entre fuentes. **Ampliar monitor** permite seguir las ejecuciones en una vista más grande.

Necesita un modelo con llamadas de herramientas y al menos **16384 de contexto**. Está seleccionado `llama3.2:3b` en el proveedor local, con comprobación real de herramientas activada. Para otra API compatible activa `verify_tools: true` y pulsa **Comprobar modelo**.

Docker Desktop está disponible en este equipo y se descargó una imagen oficial de Python fijada por digest. Los contenedores de código del modelo son temporales, sin red, archivos del equipo o credenciales del servidor. SearXNG y el lector fijo de páginas usan contenedores separados con acceso de red.

```powershell
# Prueba REAL del ejecutor, sin modelo ni consultas de pago.
.\.venv\Scripts\python.exe -X utf8 scripts/check_sandbox.py
```

Las correcciones con una ejecución posterior exitosa se guardan automáticamente como **experiencias observadas** de tu cuenta. En **Correcciones aprendidas** puedes indicar **Me sirvió** o **No es correcto**. Lo descartado deja de recuperarse como solución y genera un aviso cuando la consulta es pertinente. La ejecución exitosa y la revisión del modelo son evidencias limitadas; no garantizan exactitud.

Este aprendizaje conserva y recupera experiencias; no modifica pesos ni descarga modelos. El grafo relaciona fuentes. El análisis topológico de tablas o series queda para cuando definas los datos.

Consulta [la guía del Laboratorio y sus pruebas reales/simuladas](docs/LABORATORIO.md).

## Generar proyectos y buscar sin una clave de búsqueda de pago

Laboratorio puede guardar archivos privados por conversación, probar una copia en Docker y ofrecer **Archivos del proyecto → Descargar proyecto ZIP**. No modifica los archivos de esta aplicación. Los cambios hechos solo dentro del contenedor son temporales: deben persistirse mediante `project_write`. La prueba real con Llama creó y ejecutó un proyecto mínimo; no garantiza tareas de cualquier complejidad.

El buscador SearXNG puede ejecutarse localmente en un contenedor separado:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/setup_search.py
docker compose --env-file .local/search/search.env -f deploy/search/compose.yml up -d
.\.venv\Scripts\python.exe -X utf8 scripts/check_search.py
```

La preparación conserva tus claves y completa una URL de búsqueda vacía. Reinicia la aplicación si cambió `.env`. El lector fijo `web_read` dispone de red para extraer páginas públicas; el código generado por el modelo sigue sin red directa. Se comprobaron búsquedas reales y extracción HTTPS en Docker; no se usa un rastreador de todo internet ni se entrenan pesos.

Consulta [el paso a paso para proyectos, búsqueda y entrenamiento](docs/PROYECTOS.md).
# agentesloot
