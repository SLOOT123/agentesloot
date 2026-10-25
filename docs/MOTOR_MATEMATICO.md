# Procesos, vectores y ejecución observable

El servidor combina modelos concretos para seleccionar contexto, ejecutar herramientas y verificar resultados. Añadir todos los modelos matemáticos posibles no define una mejora medible: cada modelo tiene una función y debe evaluarse con datos del objetivo. Estas funciones están conectadas al proyecto; no son texto añadido al prompt ni entrenamiento de los pesos de Llama.

## Modelos activos

| Función | Modelo | Evidencia disponible |
| --- | --- | --- |
| Clasificar fuentes web | BM25 + vectores TF-IDF y coseno + cobertura del título | `ranking.components` en cada fuente y evento `retrieval` |
| Recuperar memoria privada | TF-IDF y coseno entre la pregunta y documentos del usuario | Método y puntuación en cada fragmento recuperado |
| Ordenar ejecución | Grafo de dependencias existente | Plan, herramientas observadas y resultados en el monitor |
| Reintentar HTTP 429 | Espera exponencial, mínimo `Retry-After` y pausa compartida por sitio | Hora del próximo intento, espera restante y presupuesto usado |
| Ejecutar código y matemáticas | Python o sh dentro de Docker | Nombre del contenedor, salida y código de salida |
| Descargar y preparar archivos | Proceso local fijo, HTTP/yt-dlp/FFmpeg/ffprobe | PID, comando, operaciones y cierre del proceso |

### Vectores y contexto

Se normalizan mayúsculas y acentos y se eliminan palabras vacías. Para cada término `t` de un documento `d`:

```text
tf(t,d)  = 1 + ln(frecuencia(t,d))         si aparece; 0 si no aparece
idf(t)   = 1 + ln((1 + N) / (1 + df(t)))
w(t,d)   = tf(t,d) * idf(t)
v(d)     = w(d) / ||w(d)||₂
cos(q,d) = v(q) · v(d)
```

El vocabulario y el IDF se calculan únicamente sobre los documentos candidatos de esa petición. Una pregunta sin términos compartidos devuelve similitud cero. La memoria solo consulta documentos del usuario autenticado; normalizar acentos no cambia los textos originales.

La búsqueda web puntúa hasta veinte candidatos únicos con:

```text
score = 0.50 * BM25 / max(BM25 de los candidatos)
      + 0.35 * coseno_TFIDF
      + 0.15 * fracción_de_términos_presentes_en_el_título
```

Si todos los BM25 son cero, su contribución es cero. Se conserva el orden original en los empates y se prioriza la diversidad de dominios. Los pesos son una configuración inicial fija, sin calibración con datos de prueba. La lectura de las páginas y la comprobación de citas siguen siendo necesarias.

Son vectores **léxicos**, basados en palabras compartidas. No son embeddings neuronales: no resuelven por sí mismos sinónimos, intención, autoridad ni veracidad. La formulación de TF-IDF y la normalización L2 pueden contrastarse con la [documentación de extracción de texto de scikit-learn](https://scikit-learn.org/stable/modules/feature_extraction.html#tfidf-term-weighting). Esta implementación usa solo la biblioteca estándar y no instala scikit-learn.

### Espera por HTTP 429

```text
espera = max(Retry-After, min(3600, 300 * 2^(fallos_del_sitio - 1))) segundos
```

La configuración activa usa **`downloads.persistent_recovery: true`**: ante HTTP 429 o fallos temporales, el objetivo sigue en cola con espera hasta completar, cancelar o encontrar un requisito externo. No se agota al llegar a los dos reintentos del modo anterior. El contador y la pausa sobreviven al reinicio. El modo finito sigue disponible con `false` y conserva dos reintentos por tipo. Un reintento manual respeta la pausa común del sitio.

`Retry-After` puede superar el tope de la parte exponencial: el mínimo solicitado por el servidor se respeta. Véase [RFC 9110, sección 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3).

Durante la espera se muestra una cuenta regresiva actualizada por las consultas del chat. No aparece una barra de transferencia animada. La cuenta regresiva es la espera programada, no una garantía de que el sitio aceptará la siguiente petición. Cada intento libera el turno del sitio para que otros objetivos puedan avanzar. Los temporales sin una fuente completa registrada se retiran antes de otro intento para evitar acumulación indefinida.

## Skills y observaciones

### Consola dentro de la misma interfaz

**Trabajo en segundo plano → Consola de ejecución** aparece abierta debajo del chat al iniciar una tarea. No hace falta abrir PowerShell ni un diálogo adicional. Se actualiza con las consultas privadas del chat y de las descargas, normalmente cada 1–2 segundos.

Presenta los comandos reales de los procesos, llamadas de herramientas, salida de Python/sh y yt-dlp, PID, nombre de contenedor, bytes, porcentaje medido, velocidad/ETA cuando el trabajador los comunica, esperas y códigos de salida. Las muestras acumuladas de consola añaden solo el texto nuevo. Un comando presentado con `$` es una observación del proceso del servidor; la consola del navegador no acepta órdenes para ejecutarlas en el sistema.

**Seguir salida** activa o desactiva el desplazamiento automático. **Ocultar/Mostrar** pliega el contenido sin detener el proceso. La vista conserva hasta 300 entradas de la conversación actual y se vacía al cambiar de conversación o cerrar sesión. Al volver, reconstruye las observaciones recientes conservadas en el servidor; no pretende conservar una transcripción ilimitada.

El indicador distingue ejecución, cola, espera programada, error, cierre y conexión interrumpida. Los procesos se identifican por trabajo para poder seguir descargas concurrentes.

Los contratos siguen en `agent_skills/`. El equipo ya usa `research`, `coding` y `math` para sus requisitos y comprobaciones. La cola de descarga carga ahora el contrato `download` y conserva su identificador, versión y criterios en `progress.skill`; un contrato inválido detiene ese trabajo. Los criterios orientan la ejecución: su presencia en el monitor no significa que se hayan cumplido.

**Ver herramientas y pasos** muestra el comando del trabajador, su PID, sus operaciones, el código de salida y su duración. Las observaciones corresponden al último proceso de ese trabajo. Un cierre del trabajador no equivale a una descarga completa: antes de publicarla se verifican los bytes y el SHA-256. Un PID sin cierre anterior al reinicio queda con estado desconocido, sin presentarlo como activo.

El código generado sigue ejecutándose en Docker sin red. Las descargas continúan como procesos locales separados, con red y herramientas fijas. No se ha movido el descargador al contenedor aislado ni se le ha dado ejecución arbitraria al modelo.

### Estrategia y recuperación

Para audio/vídeo, el trabajador compara la URL con las reglas de los extractores del yt-dlp instalado. Cuando encuentra uno específico, lo usa antes de hacer una petición HTTP genérica; así evita una consulta preliminar innecesaria a servicios como YouTube. Si ese método falla por un motivo compatible con alternativas, vuelve a examinar el enlace y los archivos ofrecidos por la página. La selección se registra como `strategy`. Las reglas siguen el orden de los [extractores de yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/__init__.py); reconocer una URL no verifica su disponibilidad.

Un error de conexión temporal, HTTP 408/500/502/503/504 o un tiempo agotado activa recuperación, con base de 5 segundos y crecimiento exponencial hasta 60 segundos; también se respeta `Retry-After`. En modo persistente continúa hasta completar o cancelar; en modo finito se limita a dos recuperaciones. El contador y la próxima hora se guardan en SQLite. Los archivos completos preservados pueden reutilizarse. Una falta de autorización o una protección no se trata como un fallo temporal.

### Descubrimiento de herramientas y objetivos bloqueados

El agente tiene ahora `tool_discover(purpose)`. Consulta las capacidades **registradas y permitidas** de su turno, su disponibilidad local/configuración, pertinencia léxica y documentación oficial de las dependencias. Puede usarlo antes de crear el plan o incluirlo como paso. El servidor también registra un inventario antes de trabajar. El descubrimiento no instala programas ni amplía permisos; una URL documental no es código autorizado para ejecutar. Cuando el contexto necesita reducirse, conserva los nombres y disponibilidad y deja el detalle en la actividad.

Una petición explícita como «Usa tool_discover…» se ejecuta en el servidor antes de permitir la respuesta del modelo: así una contestación escrita no sustituye la acción pedida. El registro distingue esta ejecución obligatoria (`server_required_action`) de una llamada elegida por el modelo. No se contabiliza la inspección local como una ejecución de red o de código.

El supervisor de descarga detecta HTTP, yt-dlp, FFmpeg, ffprobe y Node antes de ejecutar. Registra el inventario, el diagnóstico y la siguiente acción. La falta de una dependencia se distingue de la falta de acceso al sitio.

Un requisito de autorización, protección o ausencia de fuente compatible produce **`blocked`**: `objective.status` conserva el objetivo pendiente, su criterio `verified_file` y el requisito observado. No se presenta como descarga completada y no genera peticiones idénticas al reiniciar. La interfaz muestra **Reanudar tras resolver el requisito** y **Cancelar descarga**. Los trabajos terminados con respuesta parcial también se muestran como objetivos pendientes en la consola; la continuación automática persistente de esta sección corresponde al descargador, no a turnos de inferencia ilimitados.

El registro aportado por el usuario muestra un caso concreto: yt-dlp reconoce YouTube, recibe HTTP 429 y después exige confirmar la sesión. FFmpeg y Node no conceden esa autorización. La [FAQ oficial de yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) describe el uso de acceso autorizado. Esta aplicación conserva su límite actual: no importa automáticamente sesiones ni cookies del navegador. Para ese vídeo hace falta acceso habilitado o un enlace de descarga permitido; repetir herramientas no sustituye ese requisito.

Prueba local real del descubrimiento, con SQLite temporal: `.\.venv\Scripts\python.exe -u -X utf8 scripts/check_tool_discovery.py`. El informe se guarda en `.local/math-checks/tool-discovery-live.json`; no añade mensajes a las conversaciones de uso diario.

La condición de éxito sigue siendo el archivo final comprobado. Un sitio remoto que rechaza el acceso, un enlace roto o la ausencia de un formato descargable pueden impedir el objetivo; el sistema informa de esa condición y no inventa una descarga.

## Verlo mediante comandos

Desde la carpeta del proyecto:

```powershell
# Estados registrados del agente, descargas, esperas y contenedores.
.\.venv\Scripts\python.exe -X utf8 scripts/monitor_agent.py --watch --duration 60

# Ejecutar realmente los modelos matemáticos en Docker, sin llamar a la IA.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_math_pipeline.py

# Comparar el orden original, BM25 y el ranking híbrido con etiquetas existentes.
.\.venv\Scripts\python.exe -X utf8 scripts/evaluate_agent.py --output .local/math-checks/ranking.json
```

El monitor lee SQLite en modo de solo lectura. `--conversation ID` filtra una conversación y `--database RUTA` permite inspeccionar una base de pruebas. Presenta los últimos estados registrados, sin sondear si un PID histórico sigue vivo ni mostrar mensajes, contraseñas o claves.

La comprobación de Docker copia únicamente `retrieval.py` y `retry_policy.py` dentro de un contenedor temporal. Ejecuta ambos módulos reales, comprueba normas, similitudes, ranking, espera y `Retry-After`, y muestra el comando Docker y salida antes del cierre. Guarda el informe en `.local/math-checks/live.json`. Los ejemplos no hacen peticiones HTTP ni descargan vídeos. Docker Desktop debe estar disponible.

Para iniciar esa comprobación como proceso oculto y seguir su registro:

```powershell
New-Item -ItemType Directory -Force .local/math-checks | Out-Null
$mathProcess = Start-Process -WindowStyle Hidden -PassThru `
  -FilePath "$PWD\.venv\Scripts\python.exe" `
  -ArgumentList '-u', '-X', 'utf8', 'scripts/check_math_pipeline.py' `
  -WorkingDirectory "$PWD" `
  -RedirectStandardOutput "$PWD\.local\math-checks\console.log" `
  -RedirectStandardError "$PWD\.local\math-checks\console.err.log"
$mathProcess.Id
Get-Content .local/math-checks/console.log -Wait
```

`Ctrl+C` termina la lectura del registro; el proceso termina al cerrar su comprobación. No se inicia un servicio permanente.

## Alcance de la evaluación

En los cinco casos de prueba ilustrativos existentes, BM25 y el híbrido obtienen el mismo nDCG@3: **0.8**, con MRR@3 de **0.733333**. Añadir vectores no ha demostrado una mejora general sobre BM25 en ese conjunto. Hay ejemplos donde las coincidencias de palabras priorizan resultados irrelevantes: se conserva esa limitación en el informe. Para ajustar pesos o añadir embeddings hacen falta consultas representativas y etiquetas revisadas fuera del conjunto usado para evaluar.

El HTTP 429 de la captura es una limitación observada del sitio. Ni los vectores ni una skill garantizan que un servidor remoto entregue el archivo.
