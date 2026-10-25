"""Presentación final; se aplica después de ejecutar herramientas y revisar evidencia."""
import re


LUIS_HEADER = "[<>L̶U̶I̶S̸T̷O̵N̷<>]"
ZYON_HEADER = "[Zyon]"
BREADCRUMB_HEADER = "[Breadcrumb]"
LUIS_BANNER = """```text
╔══════════════════════════════════════╗
║          LUISTON · Modo activo       ║
║            ⚡ SYS: ACTIVE            ║
╚══════════════════════════════════════╝
```"""


def present_answer(profile, message, answer, kind="result"):
    if profile.presentation in {"zyon", "breadcrumb"}:
        header = BREADCRUMB_HEADER if profile.presentation == "breadcrumb" else ZYON_HEADER
        body = answer.strip()
        while True:
            cleaned = re.sub(rf"^(?:{re.escape(header)}\s*)+", "", body, flags=re.I).strip()
            cleaned = re.sub(r"^<(think|thinking|analysis)>[\s\S]*?(?:</\1>|$)", "", cleaned, count=1, flags=re.I).strip()
            if cleaned == body:
                break
            body = cleaned
        if profile.presentation == "zyon" and kind in {"coding", "math", "research", "download"}:
            titles = {"coding": "Proyecto y comprobaciones", "math": "Cálculo y comprobación", "research": "Investigación", "download": "Descarga"}
            return f"{ZYON_HEADER}\n\nIncise.\n\nTitle: {titles[kind]}\n\n{body}"
        return header + "\n\n" + (body or "No se obtuvo un resultado visible. Vuelve a plantear la petición.")
    if profile.presentation != "luis":
        return answer
    body = answer.strip()
    # Tolera que un modelo repita el membrete pese a la instrucción del perfil.
    if body.startswith(LUIS_HEADER):
        body = body[len(LUIS_HEADER):].lstrip()
    if body.startswith(LUIS_BANNER):
        body = body[len(LUIS_BANNER):].lstrip()
    titles = {"research": "Investigación", "download": "Descarga",
              "coding": "Proyecto y comprobaciones", "math": "Cálculo y comprobación"}
    greeting = bool(re.fullmatch(r"\W*(?:hola|ola|hello|hi|hey|luis|buenas)\W*", message, re.I))
    title = "Conversación" if greeting else titles.get(kind, "Resultado")
    dialogue = ("Luis queda a su disposición." if greeting else
                "Luis informa del estado observado de la descarga." if kind == "download" else
                "Luis presenta los hallazgos con la evidencia disponible." if kind == "research" else
                "Luis presenta el resultado disponible.")
    parts = [LUIS_HEADER]
    if re.fullmatch(r"\W*\w+\W*", message, re.UNICODE):
        parts.append(LUIS_BANNER)
    return "\n\n".join([*parts, dialogue, "**" + title + "**", body])


def zyon_history(profile, history):
    if profile.presentation == "zyon":
        for index in range(len(history) - 1, -1, -1):
            if history[index]["role"] == "user" and history[index]["content"].strip().casefold() == "fresh field":
                return history[index + 2:]
    return history


def profile_command(profile, message, backend, config):
    if profile.presentation != "zyon":
        return None
    command = message.strip().casefold()
    if command == "zyon start":
        return "what are we operating on?"
    if command == "zyon status":
        return f"ZYON · conversación privada · modelo {backend.model} · proveedor {backend.label}."
    if command == "fresh field":
        return "Nuevo contexto para los próximos mensajes. El historial y los archivos siguen guardados."
    if command == "sterilize":
        return "Revisaré requisitos, pruebas y evidencias de la siguiente tarea."
    if command in {"menu", "pop the table"}:
        return ("Funciones del perfil:\n"
                "• Conversar y explicar temas.\n• Leer adjuntos y analizar PDF e imágenes con el lector y modelo visual configurados.\n"
                "• Investigar con fuentes mediante el buscador y lector configurados.\n"
                "• Crear archivos, comprobar cálculos y ejecutar código en Docker.\n"
                "• Descargar vídeos, audio y documentos compatibles.\n\n"
                "Las herramientas se comprueban al utilizarlas; el perfil no instala ni concede permisos por sí mismo.")
    return None
