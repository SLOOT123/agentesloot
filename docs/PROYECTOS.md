# Crear, probar e investigar desde el chat

El objetivo es pedir una tarea concreta, obtener archivos reales, ejecutarlos en Docker y revisar el resultado en la misma conversación. Está conectado `llama3.2:3b` mediante Ollama local; se comprobó un proyecto mínimo con modelo y Docker reales. No se entrenaron pesos. Consulta [CONEXION_LOCAL.md](CONEXION_LOCAL.md) para iniciar y probar el modelo.

## Preparación

1. Abre Docker Desktop con contenedores Linux.
2. Conecta un modelo Ollama local o una API Chat Completions compatible siguiendo `README.md`. Laboratorio requiere llamadas de herramientas y al menos 16384 de contexto. En una API externa activa `verify_tools: true` y pulsa **Comprobar modelo**.
3. Para usar el buscador local sin una clave de búsqueda de pago, ejecuta desde la carpeta del proyecto:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/setup_search.py
docker compose --env-file .local/search/search.env -f deploy/search/compose.yml up -d
.\.venv\Scripts\python.exe -X utf8 scripts/check_search.py
```

La preparación genera un secreto local del buscador en `.local/search/search.env`. Crea `.env` desde la plantilla si no existe y completa `SEARXNG_URL=http://127.0.0.1:8888` solo si estaba vacío. Conserva las claves y la URL que ya tuvieras. Reinicia el servidor después de cambiar `.env`.

La imagen oficial de SearXNG se fija por digest en `deploy/search/compose.yml`. El servicio escucha solo en `127.0.0.1:8888`; tiene su propia red y no recibe las claves del agente ni el código generado por el modelo. Una primera descarga necesita internet. Usa el equipo, la conexión y los motores disponibles: no significa recursos ilimitados ni acceso a todo internet. Algunas búsquedas pueden devolver bloqueos o ningún resultado.

SearXNG permite [búsqueda JSON](https://docs.searxng.org/dev/search_api.html) cuando está habilitada. No hace falta otro modelo de IA para buscar. El mismo modelo del agente decide cuándo llamar a esta herramienta.

Para detener solo este buscador:

```powershell
docker compose --env-file .local/search/search.env -f deploy/search/compose.yml down
```

## Uso diario

1. Inicia el buscador si necesitas internet y el modelo local si usas Ollama.
2. Ejecuta `.\.venv\Scripts\python.exe -X utf8 app.py` y abre `http://127.0.0.1:8000`.
3. Entra con tu alias y contraseña; **Crear cuenta** es solo para una cuenta nueva.
4. Abre **Mi agente · privado**, selecciona proveedor y **Laboratorio · corregir y comprobar**.
5. Pide un proyecto concreto. Por ejemplo:

> Crea un proyecto pequeño en Python para calcular promedios. Guarda promedio.py, tests/test_promedio.py y README.md. Para una lista vacía devuelve None. Prueba [], [2,4] y [-2,2]. Ejecuta las pruebas en el contenedor y explica el resultado. No requiere paquetes externos.

6. Revisa **Laboratorio y actividad**: muestra herramientas, código, salidas, errores y revisión. Un saludo solo recibe una respuesta; el agente necesita una tarea para crear un proyecto.
7. Abre **Archivos del proyecto**: muestra los archivos como texto, sin ejecutarlos en el navegador. **Descargar proyecto ZIP** entrega los archivos guardados de esa conversación.
8. Si algo falla, pide que lo corrija y vuelva a comprobarlo. Si se agotó el presupuesto del turno, los archivos quedan guardados como trabajo pendiente para continuar. Una respuesta fallida no implica que todas las escrituras de archivos se deshicieran.
9. Revisa **Correcciones aprendidas** y marca las experiencias útiles o incorrectas.

El modelo dispone de `project_write` para crear/actualizar archivos y `project_read` para listar o leer fragmentos de 1000 caracteres mediante `offset`. Para probar el proyecto usa `sandbox_exec` con `project=true`. En el contenedor aparecerá en `/tmp/project`. No se montan carpetas del equipo: se reconstruye una copia desde SQLite.

Cada conversación conserva su propio proyecto. Cambiar de perfil/proveedor crea otra conversación. Los cambios que un script haga solo dentro del contenedor se pierden al terminar; el modelo debe guardarlos con `project_write` para que aparezcan en el ZIP. Los archivos son texto: hasta 32, 16000 bytes por archivo y 128000 bytes por proyecto. Son límites de este primer laboratorio para proyectos pequeños.

Python y sh están disponibles. Otros intérpretes y paquetes requieren preparar otra imagen; no se instalan automáticamente. No hay previsualización web interactiva ni servidores del proyecto publicados: los contenedores terminan tras cada ejecución.

## Internet dentro del laboratorio

`web_search` consulta SearXNG, que se ejecuta en su propio contenedor. El agente puede usar `web_read` para extraer texto de una URL exacta devuelta por ese buscador. El lector usa código fijo en otro contenedor con conexión de red; los scripts que escribe el modelo continúan con `--network=none`.

El lector:

- Acepta HTTPS en el puerto 443, sin credenciales; valida todas las IP resueltas y conecta a una IP pública fijada conservando la comprobación TLS del nombre.
- Rechaza direcciones privadas, locales, especiales y de transición. No sigue redirecciones.
- Consulta `robots.txt`; si prohíbe la lectura o no puede comprobarse con las reglas admitidas, no lee la página. Los errores de red y los bloqueos no se eluden.
- Lee como máximo 256 KiB de HTML/texto y devuelve un extracto de hasta 1000 caracteres, indicando si está truncado. No ejecuta JavaScript ni recorre enlaces. No incluye navegador ni PDF.
- Conserva los límites de contenedor y no recibe claves ni archivos del usuario. El bloqueo de destinos está en el lector fijo; no es una red de salida general para código arbitrario del modelo.

Buscar enlaces, leer una página y analizar sus datos son operaciones distintas. No se scrapea todo internet y no se promete que cada sitio permita extracción. El texto recuperado se trata como datos no confiables. Las fuentes no se convierten automáticamente en verdades ni en ejemplos de entrenamiento.

## Personalización y entrenamiento

Tus contextos pueden incorporarse mediante los prompts y **Mi memoria documental**. Las correcciones de ejecución observadas pueden recuperarse como experiencias privadas de Laboratorio. Esto personaliza las consultas sin modificar los pesos del modelo.

El ajuste de pesos sigue pendiente: necesita un modelo que lo permita, ejemplos preparados y revisados, recursos de cálculo y una evaluación independiente. No requiere necesariamente una API de pago, pero no ocurre por chatear ni por scrapear. No se descargan modelos automáticamente ni se inicia entrenamiento en segundo plano.

El grafo actual relaciona fuentes por semejanza textual. La estadística y topología avanzada sobre tablas o series siguen pendientes de definir los datos.

## Qué se probó

- Pruebas de rutas/SQLite/ZIP y aislamiento reales, con decisiones del modelo y sus ejecuciones simuladas.
- Docker real: reconstrucción de carpetas, importación Python y pruebas unittest mediante sh, además de los límites existentes.
- Búsqueda real con el SearXNG local y extracción real de documentación pública HTTPS mediante el lector fijo en Docker.
- JavaScript real con DOM y HTTP simulados: código como texto, ocultación y limpieza de archivos al cambiar de canal o expirar sesión, y descarte de respuestas atrasadas.
- Con Llama real se verificó un proyecto mínimo con archivos, ejecución Docker y revisión, además de texto, memoria e investigación. El modelo cometió errores en otras variantes de la tarea; no se garantiza autonomía general. Sigue pendiente la revisión visual en un navegador.

Comandos de comprobación:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
node tests/test_frontend.cjs
.\.venv\Scripts\python.exe -X utf8 scripts/check_sandbox.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_search.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_web_reader.py
```
