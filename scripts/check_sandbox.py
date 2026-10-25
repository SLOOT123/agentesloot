"""Pruebas REALES de Docker, sin modelo, sin consultas pagadas y sin archivos del usuario."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_core.config import ConfigFiles, Settings
from agent_core.sandbox import SandboxRunner


async def container_names():
    process = await asyncio.create_subprocess_exec("docker", "ps", "-a", "--filter", "name=community-lab-",
        "--format", "{{.Names}}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=SandboxRunner.environment())
    stdout, _ = await process.communicate()
    if process.returncode:
        raise RuntimeError("No se pudo consultar Docker. Comprueba Docker Desktop y los permisos.")
    return set(stdout.decode().splitlines())


async def main():
    config = ConfigFiles(Settings.from_env().root).agent().sandbox
    if not config.enabled:
        raise SystemExit("El sandbox está desactivado; no se ejecutaron pruebas.")
    before = await container_names()
    runner = SandboxRunner(config)
    marker = "SANDBOX_TEST_SECRET"
    previous = os.environ.get(marker)
    os.environ[marker] = "este-valor-no-debe-llegar-al-contenedor"
    try:
        isolation = await runner.run("python", """import os, socket
from pathlib import Path
assert os.geteuid() == 65534
assert 'SANDBOX_TEST_SECRET' not in os.environ
assert 'AGENT_API_KEY' not in os.environ
assert not Path('/var/run/docker.sock').exists()
assert os.statvfs('/').f_flag & os.ST_RDONLY
Path('/tmp/ejemplo.py').write_text('print(2 + 2)')
sock = socket.socket(); sock.settimeout(.5)
assert sock.connect_ex(('1.1.1.1', 80)) != 0
sock.close()
assert sum([1, 2, 3]) == 6
print('aislamiento_ok')
""")
        assert isolation["ok"] and "aislamiento_ok" in isolation["stdout"], isolation
        print("OK real: Python, no root, raíz de solo lectura, sin secretos, sin socket Docker, sin red, /tmp temporal.")
        observed = []
        first_output = asyncio.Event()
        def observe(kind, title, payload):
            observed.append((kind, payload))
            if kind == "sandbox_output" and "primera_salida" in payload.get("stdout", ""):
                first_output.set()
        runner.event = observe
        live = asyncio.create_task(runner.run("python", "import time\nprint('primera_salida')\ntime.sleep(2)\nprint('ultima_salida')"))
        await asyncio.wait_for(first_output.wait(), timeout=10)
        assert not live.done(), "La salida debe observarse antes de que termine el contenedor."
        streamed = await live
        assert streamed["ok"], streamed
        assert observed[0][0] == "sandbox_started" and observed[-1][0] == "sandbox_finished", observed
        assert observed[-1][1]["status"] == "complete" and "ultima_salida" in observed[-1][1]["stdout"], observed
        assert len({payload["container"] for _, payload in observed}) == 1
        print("OK real: monitor recibe consola antes del cierre, nombre propio y resultado final del contenedor.")
        failed = await runner.run("python", "print(1 / 0)")
        assert not failed["ok"] and "ZeroDivisionError" in failed["stderr"], failed
        repaired = await runner.run("python", "values = [1, 2, 3]\nassert sum(values) / len(values) == 2\nprint(2)")
        assert repaired["ok"], repaired
        print("OK real: se observó un error Python y se ejecutó después un script corregido con una aserción.")
        shell = await runner.run("sh", "test \"$(id -u)\" = 65534\nprintf 'shell_ok\\n'")
        assert shell["ok"] and "shell_ok" in shell["stdout"], shell
        print("OK real: comandos sh dentro del contenedor.")
        files = [{"path": "src/promedio.py", "content": "def promedio(v):\n    return sum(v)/len(v) if v else None\n"},
                 {"path": "tests/test_promedio.py", "content": "import unittest\nfrom src.promedio import promedio\nclass Prueba(unittest.TestCase):\n    def test_promedio(self):\n        self.assertIsNone(promedio([]))\n        self.assertEqual(promedio([2,4]), 3)\n"}]
        project = await runner.run("python", "from src.promedio import promedio\nassert promedio([]) is None\nassert promedio([2,4]) == 3\nprint('proyecto_python_ok')", files=files)
        assert project["ok"] and "proyecto_python_ok" in project["stdout"], project
        project_shell = await runner.run("sh", "python -B -m unittest discover -s tests -v", files=files)
        assert project_shell["ok"] and "OK" in project_shell["stderr"], project_shell
        print("OK real: proyecto con carpetas, importación Python y pruebas unittest mediante sh, sin montar carpetas del equipo.")
        missing = await runner.run("sh", "comando_de_prueba_que_no_existe")
        assert missing["exit_code"] == 127 and missing.get("stderr") and missing.get("error") != "docker_unavailable", missing
        print("OK real: un comando inexistente conserva su error para poder corregirlo.")
        limited = SandboxRunner(config.model_copy(update={"max_output_bytes": 1000}))
        flood = await limited.run("python", "while True: print('x' * 1000)")
        assert flood["error"] == "output_limit", flood
        deadline = SandboxRunner(config.model_copy(update={"timeout_seconds": 2}))
        timeout = await deadline.run("python", "import time; time.sleep(30)")
        assert timeout["error"] == "script_timeout", timeout
        print("OK real: límites de salida y tiempo detienen el contenedor.")
        task = asyncio.create_task(runner.run("python", "import time; time.sleep(30)"))
        await asyncio.sleep(1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert observed[-1][0] == "sandbox_finished" and observed[-1][1]["status"] == "cancelled", observed[-1]
        after = await container_names()
        assert not after - before, "Quedaron contenedores de estas pruebas."
        print("OK real: cancelación y limpieza; no quedaron contenedores nuevos de laboratorio.")
    finally:
        if previous is None:
            os.environ.pop(marker, None)
        else:
            os.environ[marker] = previous


if __name__ == "__main__":
    asyncio.run(main())
