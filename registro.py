import logging
from datetime import datetime

# Configurar el registro de auditoría sin rodeos
logging.basicConfig(
    filename='server_audit.log',
    level=logging.INFO,
    format='%(asctime)s - [AUDITORÍA] - %(message)s'
)

def registrar_subida(usuario: str, ip: str, proyecto: str):
    log_msg = f"¡Proyecto desplegado! Usuario: {usuario} | IP: {ip} | Proyecto: {proyecto}"
    logging.info(log_msg)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {log_msg}")

if __name__ == "__main__":
    print("Sistema de auditoría listo para atrapar cualquier anomalía, jefe.")
    # Ejemplo de uso cuando alguien sube algo:
    registrar_subida("banda_anonima", "192.168.1.50", "mi-web-personal")