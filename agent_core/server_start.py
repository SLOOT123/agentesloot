"""Arranque único: reconoce el servidor de este proyecto y evita otra copia."""
import hashlib
import json
import socket
import time

import httpx
import uvicorn


SERVICE_NAME = 'fash-toch-community-agent'


def instance_key(root, database):
    value = str(root.resolve()).casefold() + '\0' + str(database.resolve()).casefold()
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def browser_url(host, port):
    target = '127.0.0.1' if host == '0.0.0.0' else '::1' if host == '::' else host
    return f'http://[{target}]:{port}' if ':' in target else f'http://{target}:{port}'


def is_running(url, instance):
    try:
        with httpx.Client(timeout=.7, trust_env=False, follow_redirects=False) as client:
            with client.stream('GET', url + '/healthz') as response:
                if response.status_code != 200:
                    return False
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 512:
                        return False
        result = json.loads(data)
        return (isinstance(result, dict) and result.get('service') == SERVICE_NAME
                and result.get('instance') == instance and result.get('status') == 'ok')
    except (httpx.HTTPError, ValueError):
        return False


def serve(app, host='127.0.0.1', port=8000):
    url = browser_url(host, port)
    instance = app.state.instance_key

    def already_running():
        print('El servidor de este proyecto ya está funcionando.', flush=True)
        print('Abre ' + url + ' · No se inició una segunda instancia.', flush=True)
        return 0

    if is_running(url, instance):
        return already_running()
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
        except OSError as error:
            # Otra copia puede estar entre bind y el final de su arranque.
            for _ in range(4):
                if is_running(url, instance):
                    return already_running()
                time.sleep(.15)
            print(f'No se pudo abrir el puerto {port} (error {error.errno}).', flush=True)
            print('Lo ocupa otro proceso o Windows lo tiene reservado. No se detuvo ningún proceso.', flush=True)
            return 1
        print('Iniciando el servidor en ' + url, flush=True)
        configuration = uvicorn.Config(app, host=host, port=port)
        uvicorn.Server(configuration).run(sockets=[listener])
    return 0
