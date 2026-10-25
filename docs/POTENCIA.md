# Mejorar el agente con Llama, LangChain y LangGraph

Sí pueden combinarse. LangChain y LangGraph organizan las consultas y las herramientas; el modelo sigue siendo quien interpreta la petición y propone acciones. No entrenan automáticamente los pesos de Llama ni garantizan que cualquier petición se complete.

## Qué está funcionando y qué lo limita

- El modelo local configurado es `llama3.2:3b`, servido por Ollama. Se han observado errores en argumentos de herramientas, elección de lenguaje y tareas de varios pasos. Una prueba simple aprobada no garantiza que otras tareas funcionen.
- El perfil predeterminado **Mi agente** tenía solo calculadora y memoria. Ahora también ofrece `web_search` y `web_read`. La búsqueda se usa cuando el modelo la solicita; un saludo no fuerza una consulta. Si el modelo no admite herramientas, este perfil conserva el chat, pero no puede hacer esas llamadas.
- **Investigador** fuerza una búsqueda inicial. **Laboratorio · corregir y comprobar** permite archivos, scripts Python/sh en Docker y correcciones acotadas. Cambiar de perfil crea otra conversación; los proyectos no se comparten entre conversaciones.
- La memoria actual busca coincidencias de términos en textos privados. Todavía no hay embeddings ni índice vectorial.
- Los límites actuales son ocho herramientas y ocho consultas al modelo por turno, 180 segundos globales y 15 segundos por script. El perfil Agente añade un DAG propio con dependencias y cierre de acciones. Algunas tareas largas requerirán persistencia y reanudación del flujo; consulta [AGENTE_GRAFOS.md](AGENTE_GRAFOS.md).
- Las herramientas son capacidades concretas: todavía no hay un descargador de música/video, un navegador autónomo ni instalación automática de paquetes.

El error o la respuesta concreta siguen siendo necesarios para saber si una tarea falla por configuración, falta de herramienta, falta de datos o decisión del modelo.

Archivos de esta mejora:

| Archivo | Cambio |
| --- | --- |
| `profiles/general.json` | Habilita búsqueda y lectura web en Mi agente; conserva la ejecución de scripts en Laboratorio. |
| `prompts/general.md` | Explica cuándo buscar, cómo usar fuentes y cuándo seleccionar Laboratorio. |
| `agent_core/tools.py` | El error por falta de buscador ahora explica la configuración necesaria para cualquier perfil. |
| `scripts/check_agent_live.py` | Añade `--general-web` para verificar la búsqueda con el modelo real y una base temporal. |
| `tests/test_project.py` | Comprueba búsqueda opcional, permisos y chat sin requerir el buscador; modelo y buscador simulados. |
| `README.md`, `docs/AGENTE.md`, `docs/CONEXION_LOCAL.md`, `docs/REVISION.md` | Actualizan el uso, los límites y la evidencia de pruebas. |

## Qué aporta cada componente

| Componente | Para qué sirve | Estado aquí |
| --- | --- | --- |
| Ollama + Llama | Ejecutar el modelo local y producir respuestas/llamadas de herramientas | Conectado mediante el adaptador propio. |
| LangChain | Conectar modelos, herramientas y recuperación de documentos mediante interfaces comunes | No instalado ni integrado; parte de esa función ya existe en `providers.py`, `tools.py` y `service.py`. |
| LangGraph | Definir pasos, bifurcaciones, reintentos y puntos de reanudación de una tarea | Pendiente; útil para tareas largas. El bucle actual ya ejecuta herramientas y revisión dentro de un turno. |
| Embeddings + recuperación documental (RAG) | Buscar fragmentos por semejanza semántica y añadirlos al contexto | Pendiente. No modifica los pesos. |
| Transformers + PEFT/LoRA | Ajustar un modelo con ejemplos preparados para un objetivo concreto | Pendiente; no se ha iniciado entrenamiento. |
| Estadística y topología | Analizar datos con algoritmos y métricas adecuados al problema | El grafo actual mide semejanza textual. Falta definir datos y objetivo para análisis avanzado. |

El grafo de tareas de LangGraph describe el orden de ejecución. No realiza por sí solo análisis topológico de datos, ni vuelve más exacta la investigación por usar un grafo.

Documentación oficial: [LangChain](https://docs.langchain.com/oss/python/langchain/overview), [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview), [persistencia de LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence), [ajuste con Transformers](https://huggingface.co/docs/transformers/training) y [LoRA con PEFT](https://huggingface.co/docs/peft/main/en/conceptual_guides/lora).

## Orden de mejora para este proyecto

1. Registrar tareas concretas que hoy fallen y definir un resultado comprobable: archivos esperados, pruebas aprobadas o afirmaciones respaldadas por fuentes. Comparar éxito, tiempo y errores entre modelos que quepan en el equipo.
2. Mejorar la memoria con fragmentos e índice semántico privado. Recuperar por usuario y conservar referencias para revisar las fuentes; medir que la respuesta utiliza los documentos correctos.
3. Incorporar LangGraph para el flujo **planificar → buscar/recuperar → ejecutar → comprobar → corregir → responder**, con guardado de avances. Reutilizar los adaptadores, herramientas, eventos, autenticación y contenedores actuales. Los puntos de reanudación deben comprobar el propietario de la conversación y evitar repetir escrituras o ejecuciones ya realizadas. El flujo debe tener límites y un estado pendiente cuando no pueda continuar.
4. Añadir las herramientas que una tarea necesite, con formatos de entrada/salida y comprobaciones concretas. Un cambio de prompt no instala un descargador ni un intérprete.
5. Preparar ejemplos revisados de tareas correctas y errores resueltos. Separar entrenamiento y evaluación; solo entonces valorar LoRA y comparar el modelo ajustado con el original. No entrenar directamente con páginas o respuestas sin revisar.

Las correcciones recordadas pueden ayudar a evitar fallos similares, pero no cambian los pesos automáticamente. LoRA puede especializar un modelo en un formato o tipo de tarea; no sustituye datos actuales ni herramientas que falten.

## Probar ahora

Mantén Ollama y Docker Desktop abiertos. Desde la carpeta del proyecto, inicia el buscador preparado y la aplicación:

```powershell
docker compose --env-file .local/search/search.env -f deploy/search/compose.yml up -d
.\.venv\Scripts\python.exe -X utf8 app.py
```

Si la aplicación ya estaba iniciada, detenla con Ctrl+C en su terminal antes de volver a ejecutarla. Abre `http://127.0.0.1:8000`, inicia sesión, recarga la página y selecciona **Mi agente · privado**, proveedor **Ollama local**, perfil **Mi agente**. Prueba:

> Busca en la documentación oficial de Python para qué sirve unittest y cita las fuentes encontradas.

Comprueba que aparezcan enlaces en **Fuentes consultadas** y una llamada a `web_search` en la actividad. La ausencia de resultados no debe sustituirse por enlaces inventados. Leer páginas con `web_read` requiere Docker y puede fallar por robots, red o formato; los extractos no equivalen a una página completa.

Para ejecutar código, elige **Laboratorio · corregir y comprobar** y empieza con un proyecto pequeño sin paquetes externos, como el ejemplo de [PROYECTOS.md](PROYECTOS.md).

Comprobación automatizada con modelo y búsqueda reales, usando SQLite temporal:

```powershell
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_agent_live.py --backend local --general-web
```

Esta prueba comprueba texto, memoria y que el modelo solicite una búsqueda con fuentes. No evalúa toda la exactitud de la respuesta ni inicia entrenamiento. Con Ollama y SearXNG locales no necesitas una clave de API de pago. El equipo y la conexión siguen consumiendo recursos; la búsqueda depende de los motores disponibles. Para usar un proveedor externo, las credenciales se configuran en el servidor.
