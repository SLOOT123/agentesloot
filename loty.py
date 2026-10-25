import psutil
import time

def check_system_health():
    cpu_usage = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    
    print(f"[MONITOREO] CPU: {cpu_usage}% | Memoria: {memory}% | Disco: {disk}%")
    if cpu_usage > 85:
        print("¡ALERTA, JEFE! El CPU está al límite, esta madre va a tronar.")

if __name__ == "__main__":
    print("Iniciando vigilancia de recursos...")
    while True:
        check_system_health()
        time.sleep(5)