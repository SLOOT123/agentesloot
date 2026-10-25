# Breadcrumb: instrucciones y perfil

Hay dos configuraciones independientes:

- `AGENTS.md` guía a Codex cuando trabaja en este proyecto.
- `profiles/breadcrumb.json` carga `prompts/base.md` y `prompts/breadcrumb.md`
  en el agente de la aplicación.

Ambas fijan español, trato Baggute, entregas completas, investigación cuando
corresponda y comprobaciones con resultados observados. Un MD no concede permisos,
elimina restricciones del proveedor ni garantiza que cualquier script sea correcto.
La calidad depende también del modelo, las herramientas y las pruebas ejecutadas.

## Usar en Codex

Inicia una sesión de Codex en esta carpeta para cargar `AGENTS.md`. En esta
conversación ya se leyó su contenido. La guía es local al proyecto; no modifica
las instrucciones globales de otros repositorios.

Referencia oficial: [instrucciones con AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

## Usar en la aplicación

1. Inicia el servidor con el Python del proyecto:

   ```powershell
   .\.venv\Scripts\python.exe -X utf8 app.py
   ```

2. Si ya había un servidor iniciado antes de instalar el perfil, detenlo con
   `Ctrl+C` en su terminal y vuelve a iniciarlo para cargar la presentación nueva.
3. Abre `http://127.0.0.1:8000`, recarga la página y entra en **Mi agente · privado**.
4. Selecciona **Breadcrumb · investigar y comprobar** en **Perfil**. Se crea una
   conversación nueva. El perfil predeterminado de la aplicación sigue siendo Equipo.
5. Envía una tarea concreta y consulta **Trabajo en segundo plano** para observarla.

El servidor añade `[Breadcrumb]` a la respuesta final después de procesar las
herramientas. Los argumentos JSON, las citas y los resultados de comprobación
mantienen sus contratos. El perfil comparte las herramientas del Equipo y usa
sus colas persistentes; no instala otro buscador ni otro modelo.

## Dependencias y comprobaciones

- El modelo de Ollama debe estar disponible y admitir las llamadas de herramientas
  requeridas; el perfil solicita al menos 16384 de contexto.
- La búsqueda requiere `SEARXNG_URL` en el servidor. `web_read` y `sandbox_exec`
  requieren el Docker configurado. Los secretos se gestionan en `.env`, nunca
  en este MD ni en el chat.
- Los proyectos generados se ejecutan con el aislamiento y los presupuestos
  existentes. La compatibilidad con otros sistemas requiere comprobarla aparte.
- El servidor debe permanecer ejecutándose para procesar trabajos. El estado y
  los resultados se guardan; cerrar la página no convierte el navegador en un
  servicio autónomo.

Para comprobar integración, presentación y cola con proveedores simulados y
SQLite temporal:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p 'test_breadcrumb.py'
```

Para probar investigación y código con el modelo, buscador y Docker reales,
sin añadir mensajes a las cuentas de uso diario:

```powershell
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_team.py --profile breadcrumb
.\.venv\Scripts\python.exe -u -X utf8 scripts/check_team.py --profile breadcrumb --code
```

Estos comandos conservan informes en `.local/team-checks/breadcrumb-research.json`
y `.local/team-checks/breadcrumb-coding.json`. Un informe parcial o fallido no
demuestra éxito. Cada entrega debe distinguir pruebas reales, simuladas y pendientes.

`complete` en la cola indica que terminó el procesamiento. Consulta también
`team.status`, `review` y las comprobaciones del proyecto: una investigación puede
terminar su ejecución y seguir bloqueada por falta de evidencia.

## Verificación observada el 22 de octubre de 2026

- Pasaron 23 pruebas de Breadcrumb, ZYON, Luis, API en segundo plano y errores de
  prompts. Usaron SQLite temporal y proveedores simulados.
- La investigación real con `llama3.2:3b`, SearXNG y el lector de Docker terminó
  con `team_sources_reviewed`, tres páginas leídas y citas conservadas.
- La programación real guardó `promedio.py` y `test_promedio.py`, y ejecutó dos
  pruebas en Docker: promedio de `[2, 4]` y rechazo de lista vacía. Se observó
  `PRUEBAS_COMPROBADAS: 2`, salida cero y `project_unverified: false`.
- El buscador existente `fash-toch-search-searxng-1` estaba detenido; se comprobó
  que pertenecía a este proyecto y se inició antes de repetir la prueba real.

Los informes conservados describen estas ejecuciones concretas; no acreditan la
corrección de futuros scripts ni de otros entornos.
