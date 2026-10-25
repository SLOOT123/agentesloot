# Revisión y alcance de esta integración

Revisión del 15 de octubre de 2026. Referencia encontrada en `C:/Users/sloty/Desktop/AGENTE ZERO/agent-zero-main`. Se revisaron su `README.md`, `agent.py`, `models.py`, la organización de perfiles y el prompt principal. Esa copia no se modificó ni se instaló: sirve como referencia arquitectónica. Esta integración no ejecuta ni reproduce todo Agent Zero.

## Lo que había antes

| Estado inicial | Componente | Evidencia |
| --- | --- | --- |
| Implementado | Diseño, logo, video, transparencia, conexiones y navegación móvil | `templates/`, `static/style.css`, `static/effects.js`, `static/chat.js`. |
| Implementado | Chat entre usuarios | `static/app.js` enviaba `POST /api/messages`; consultaba `GET /api/messages` cada tres segundos. `app.py` guardaba todos los mensajes en una tabla compartida. |
| Implementado | Persistencia básica | SQLite tenía cuatro usuarios y un mensaje comunitario; no había tablas de conversaciones privadas. Se contaron filas sin mostrar sus contenidos. |
| Incompleto | Autenticación | `/auth` registraba un alias o actualizaba su IP, pero no verificaba la contraseña. Se almacenaba en texto y se confiaba en `session_user` sin firma ni validación del servidor. |
| Incompleto | Nuevo chat | `clearScreen()` ocultaba mensajes en esa vista; no creaba una conversación persistente. |
| Apariencia sin función real | Respuestas de IA | La interfaz decía Asistente, pero ninguna ruta consultaba un modelo. `gpt.html` era un prototipo independiente con respuesta por temporizador y un error de sintaxis en `clearChat`; no se servía desde `app.py`. Se conservó como referencia y no se integra. |
| Independiente | `registro.py`, `loty.py` | Auditoría de ejemplo y monitoreo con `psutil`, respectivamente. No estaban conectados al envío del chat; `psutil` no formaba parte de las dependencias del servidor. No se modificaron. |
| Pendiente | Agente, adaptadores, perfiles, memoria, fuentes y límites | No existía esa cadena de ejecución. |

El primer componente pendiente para añadir chats privados de forma coherente era una identidad verificable. Por eso la autenticación se corrigió antes de conectar las rutas del agente.

## Lo implementado ahora

| Componente | Resultado |
| --- | --- |
| Acceso | Contraseñas scrypt, registro explícito, sesiones opacas con caducidad, cierre con revocación, cookies HttpOnly y SameSite, CSRF y comprobación de origen. |
| Canal comunitario | Se conservan `/api/messages`, el formato anterior con identificadores añadidos y la actualización periódica. Se muestran los últimos 200 mensajes. |
| Conversaciones del agente | Tablas, rutas e historial separados; cada operación comprueba el usuario propietario. El canal comunitario no se envía al modelo. |
| Núcleo | Recepción, perfil, prompt, memoria pertinente, historial acotado, proveedor, herramientas limitadas y respuesta. El turno usuario/asistente se guarda completo en una transacción. |
| Adaptadores | Ollama nativo y API Chat Completions compatible. Modelo, URL, protocolo y variable de credencial están fuera del núcleo. |
| Capacidades | Diagnóstico por modelo con evidencia: metadatos de Ollama o prueba de texto; prueba opcional de llamadas de herramienta. No se presume soporte universal. |
| Memoria | Hasta 50 textos privados por usuario, recuperación léxica y fuentes con referencia al documento. |
| Investigación | SearXNG JSON local en un contenedor separado y lector fijo HTTPS en Docker, con destinos públicos, robots y extractos acotados. Sin navegador autónomo ni rastreo recursivo. |
| Límites | Tiempo global y de red, contexto, tamaño de entrada/salida, pasos, herramientas, concurrencia, frecuencia e identificadores de reintento. |
| Interfaz | Selector comunitario/agente, perfiles/proveedores, conversaciones, comprobación de modelo y memoria. Conserva los recursos gráficos existentes. |
| Laboratorio | Scripts Python/sh en Docker, registro dentro del chat, reintentos asociados al fallo y revisión final acotada. |
| Experiencias | Correcciones observadas, recuperación por cuenta y valoración. Las descartadas generan avisos y no se reutilizan como soluciones. |
| Relaciones | Grafo de semejanza entre fuentes, componentes, ciclos y densidad; alcance descriptivo. |
| Proyectos generados | Archivos privados en SQLite por conversación, escritura por lotes, lectura, ZIP, copia temporal para pruebas en Docker y vista segura dentro del chat. |

## Qué todavía requiere configuración o una etapa posterior

1. **Modelo local conectado:** tras instalar el usuario Ollama, se descargó `llama3.2:3b` y se configuró su API local con 16384 de contexto, temperatura 0 y comprobación real de herramientas. Se verificaron inferencia, memoria, un proyecto mínimo en Docker e investigación. El proveedor externo sigue sin configurar y no se inició entrenamiento.
2. **Investigación disponible:** se descargó la imagen oficial de SearXNG fijada por digest y se comprobaron consultas reales al contenedor local desde la herramienta de búsqueda. `setup_search.py` prepara la URL en `.env` solo si estaba vacía y conserva los secretos existentes. Hay que mantener el servicio iniciado y reiniciar la aplicación si cambió el entorno. Se comprobó lectura HTTPS real en el lector fijo Docker e investigación con fuentes y respuesta de Llama real; esa última prueba usó búsquedas, sin afirmar lectura de páginas completas. Mi agente ahora dispone también de búsqueda y lectura cuando el modelo las solicita; antes este perfil solo tenía calculadora y memoria. No fuerza búsquedas en cada saludo.
3. **Memoria avanzada:** embeddings, índices vectoriales, archivos PDF/Office, resumen automático y políticas de retención pueden añadirse cuando se necesiten. La memoria actual funciona con textos y coincidencia de términos.
4. **Herramientas adicionales:** Laboratorio ejecuta Python/sh en contenedores aislados sin red ni archivos del equipo. El acceso web usa SearXNG y un lector fijo en otro contenedor con red y validación de destinos. Otros lenguajes y dependencias requieren preparar imágenes. No hay terminal del host, navegador autónomo, MCP ni subagentes.
5. **Pesos propios:** la arquitectura puede apuntar a un modelo propio servido por Ollama o una API compatible. Se descargó el modelo público Llama indicado arriba, pero no se entrenó ni ajustó ningún modelo. LangChain/LangGraph e índices vectoriales siguen pendientes; el orden propuesto está en [POTENCIA.md](POTENCIA.md).
6. **Despliegue público:** este arranque es local. Para publicarlo hacen falta HTTPS, `COOKIE_SECURE=true`, hosts autorizados, gestión de secretos y copias, recuperación de cuentas y decisiones sobre acceso/registro. No se implementaron recuperación de contraseña ni administración de usuarios.

## Pruebas y su alcance

**Pruebas automatizadas:** 57 pruebas Python aprobadas, incluidas rutas FastAPI, middleware, autenticación, SQLite y ZIP reales en archivos temporales. Incluyen migración, conservación del chat, persistencia, aislamiento, sesiones, límites, concurrencia, idempotencia, corrección/revisión, experiencias, grafo, proyectos, destinos del lector, conservación de configuración, temperatura opcional, errores de diagnóstico y normalización de argumentos JSON. Incluyen búsqueda solicitada por el modelo en Mi agente, ausencia de permisos de ejecución en ese perfil y chat sin forzar búsqueda ni requerir SearXNG. Las llamadas externas y los contenedores dentro de estas pruebas están simulados; Docker, modelo y red se comprobaron aparte.

**Simuladas:** los proveedores y SearXNG se sustituyen por `httpx.MockTransport` en la suite. Así se prueban los adaptadores, el formato de solicitudes, errores, herramientas y fuentes sin consumir modelos ni cuota. La prueba Node ejecuta los módulos JS reales con DOM y HTTP simulados y comprueba que una lectura atrasada o una respuesta privada no invada el otro canal, incluyendo archivos y descarga. No es una prueba visual de navegador.

**HTTP real local:** `scripts/check_http.py` comprueba la respuesta de Uvicorn, login, archivos JS/CSS, logo, intervalos del video y rechazo de accesos sin sesión. No crea usuarios ni mensajes y no llama a modelos.

**Docker real:** `scripts/check_sandbox.py` ejecutó Python y sh en la imagen oficial fijada por digest. Comprobó usuario no root, raíz de solo lectura, ausencia de secretos y socket Docker, red deshabilitada, escritura temporal, error y ejecución posterior corregida, comando inexistente, límite de salida, tiempo máximo, cancelación y limpieza. No se usó un modelo real para decidir los scripts. Se corrigió el cierre de tuberías del cliente Docker en Windows tras detectar un transporte abierto.

**Proyectos y web reales:** el ejecutor reconstruyó archivos con carpetas, importó módulos propios y ejecutó unittest mediante sh, sin montar carpetas del equipo. `scripts/check_search.py` obtuvo tres resultados públicos del SearXNG local. `scripts/check_web_reader.py` rechazó un destino local y extrajo 1000 caracteres de una respuesta HTTPS de 53953 bytes de documentación pública, marcando truncamiento. No ejecutó JavaScript ni entrenó pesos.

**Modelo real:** `scripts/check_agent_live.py --backend local --laboratory --research` pasó con `llama3.2:3b`. Comprobó texto, respuesta con un dato nuevo de memoria, creación de `saludo.py`, ejecución en Docker con salida `PRUEBA_REAL_OK`, revisión final e investigación con fuentes. Usó SQLite temporal y no creó cuentas en la base real. Durante la integración se observaron argumentos JSON codificados como strings, confusión de lenguaje, rechazo de contexto disponible y revisiones innecesarias; se adaptaron el proveedor y los prompts. Hubo variantes que el modelo no completó ni reparó: pasar una tarea mínima no demuestra autonomía o exactitud general. El ciclo de aprendizaje de un fallo corregido sigue sin verificarse con este modelo real.

**Búsqueda real desde Mi agente:** después de ampliar el perfil, `scripts/check_agent_live.py --backend local --general-web` aprobó texto, memoria y una búsqueda solicitada por el propio modelo al SearXNG real. La búsqueda tardó aproximadamente 8 segundos con el modelo cargado. Se comprobó la llamada y la devolución de fuentes, sin afirmar lectura de páginas completas ni evaluar toda la exactitud del texto generado. Usó SQLite temporal, sin cuentas o mensajes de prueba en la base real.

**Datos existentes:** se migraron los cuatro usuarios a hashes y se conservó el mensaje comunitario; hay una copia previa en `.local/backups/`. Los mensajes privados de las pruebas usan bases temporales.

**No verificado visualmente:** no había un navegador conectado a las herramientas de esta sesión. Logo, video y efectos se conservaron; es necesaria la revisión visual manual de los controles nuevos. Las pruebas del modelo real anteriores no sustituyen una revisión visual.
