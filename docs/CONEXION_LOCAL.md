# Conexión local con Ollama y Llama

Se instaló el modelo `llama3.2:3b` y se seleccionó en `config/agent.json`, dentro de `backends.local`. La dirección es `http://127.0.0.1:11434`, con adaptador `ollama`, contexto 16384, `temperature: 0` y `verify_tools: true`. No se necesita una clave API externa para esta conexión local.

Ollama es el programa que sirve el modelo; Llama es el modelo descargado. Instalar solamente el programa deja vacía la lista de modelos. El modelo usado para esta primera prueba ocupa aproximadamente 2 GB; su [ficha oficial](https://ollama.com/library/llama3.2) declara texto y herramientas. La comprobación de tu proyecto realiza además una llamada real para comprobar herramientas.

## Iniciar y probar

1. Mantén Ollama abierto. Para ver los modelos desde la terminal de VS Code:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" list
```

Se usa la ruta completa porque una terminal abierta antes de instalar Ollama puede no encontrar `ollama`. Puedes cerrar y volver a abrir VS Code para actualizar su PATH.

2. Comprueba la conexión y capacidades desde tu proyecto:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agent_core.check --backend local
```

`tools_evidence: prueba de llamada real` junto con `tools: true` indica que el modelo llamó correctamente la herramienta de comprobación. La evidencia de texto puede aparecer como declarada por Ollama; esta comprobación no garantiza todas las tareas futuras.

3. Si ya tienes tu servidor abierto, detenlo con Ctrl+C en su terminal y vuelve a iniciarlo:

```powershell
.\.venv\Scripts\python.exe -X utf8 app.py
```

4. Abre `http://127.0.0.1:8000`, entra con tu alias y contraseña y recarga la página para renovar la lista de proveedores. Abre **Mi agente · privado**, selecciona **Ollama local** y pulsa **Comprobar modelo**.

5. Prueba primero el perfil **Mi agente** escribiendo: «Hola, explícame qué puedes hacer». Este perfil también puede buscar cuando se lo pidas, con SearXNG iniciado según `PROYECTOS.md`. Para usar scripts selecciona **Laboratorio · corregir y comprobar** y mantén Docker Desktop abierto. **Investigador** hace una búsqueda inicial en cada turno. Consulta [POTENCIA.md](POTENCIA.md) para mejorar el agente y distinguir LangChain, LangGraph y entrenamiento.

| Dirección | Función |
| --- | --- |
| `http://127.0.0.1:8000` | Tu login y chat. |
| `http://127.0.0.1:11434` | API local del modelo, utilizada por el servidor Python. |
| `http://127.0.0.1:8888` | Buscador local SearXNG, cuando esté iniciado. |

El navegador habla con tu aplicación; `agent_core/providers.py` se comunica con Ollama. No pegues claves ni cambies JavaScript para conectar este modelo.

## Repetir pruebas con el modelo real

```powershell
# Texto y recuperación de memoria; SQLite temporal, modelo real.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_agent_live.py --backend local

# Comprueba además que Mi agente use la búsqueda solicitada; inicia SearXNG primero.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_agent_live.py --backend local --general-web

# Añade proyecto en Docker e investigación real; inicia Docker y SearXNG primero.
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_agent_live.py --backend local --laboratory --research
```

Estas pruebas no usan un modelo simulado y no escriben cuentas ni mensajes en tu base real. Verifican los resultados observados; si el modelo no llama a las herramientas necesarias, la prueba informa del fallo. No inician entrenamiento.

En este equipo se comprobaron realmente texto, recuperación de un nombre de proyecto guardado en memoria, creación de `saludo.py`, ejecución del archivo en Docker, revisión final e investigación con fuentes. La ejecución de laboratorio produjo `PRUEBA_REAL_OK`. Los tiempos observados con el modelo cargado fueron aproximadamente 1–2 segundos para respuestas simples, 5 para el proyecto mínimo y 8 para investigar; no son garantías de rendimiento.

La primera carga tardó más y agotó el plazo anterior. Se ajustaron los límites a 90 segundos por solicitud y 180 por turno. Un fallo de conexión o tiempo durante la prueba de herramientas se informa como tal, sin presentarlo como ausencia de capacidad del modelo.

También se observaron errores del modelo: listas y booleanos enviados como texto JSON, confusión entre comandos de terminal y código Python, y consultas web innecesarias en una revisión. El adaptador convierte solo los tipos JSON declarados por el esquema y conserva las validaciones de herramientas. Se aclararon los prompts y se configuró temperatura 0. La prueba mínima pasó después de estos ajustes; el modelo de 3B puede seguir fallando en instrucciones complejas y no se ha demostrado autocorrección general.

Este modelo es de texto. La entrada de imágenes, los descargadores de audio/video y el entrenamiento de pesos siguen siendo capacidades distintas pendientes. Un modelo conectado no añade herramientas que el proyecto todavía no tenga.
