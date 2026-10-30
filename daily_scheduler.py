"""Agendador diário: roda main.py uma vez por dia no horário DAILY_RUN_TIME do .env.

- Se o computador estava desligado no horário, roda assim que ligar (no mesmo dia).
- Se a execução falhar, tenta de novo a cada RETRY_MINUTES, até MAX_RUNS_PER_DAY vezes;
  como main.py tem checkpoint, cada nova tentativa retoma de onde parou.
- Cada execução roda em um processo separado: se travar ou cair, o agendador continua vivo
  e a memória é liberada ao fim de cada vídeo.
- Estado em logs/scheduler_state.json; log em logs/scheduler.log.

Uso:
    python daily_scheduler.py          # fica rodando (use start_scheduler.bat / .sh)
    python daily_scheduler.py --once   # verifica uma vez e sai (para testar)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
STATE_PATH = PROJECT_DIR / "logs" / "scheduler_state.json"
POLL_SECONDS = 60
# Porta local usada como trava: só um agendador por computador.
SINGLE_INSTANCE_PORT = 47621
RUN_TIMEOUT_SECONDS = 3 * 3600

log = logging.getLogger("scheduler")


def run_time() -> dtime:
    raw = (os.getenv("DAILY_RUN_TIME") or "08:00").strip()
    try:
        hours, minutes = (int(x) for x in raw.split(":"))
        return dtime(hours, minutes)
    except ValueError:
        raise SystemExit(f"DAILY_RUN_TIME inválido: {raw!r} (use HH:MM, ex.: 08:00)")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def should_run(now: datetime, scheduled: dtime, state: dict,
               retry_minutes: int, max_runs: int) -> bool:
    """Decide se deve rodar agora. state = {"day", "runs", "done", "last_attempt"}."""
    if now.time() < scheduled:
        return False
    today = now.date().isoformat()
    if state.get("day") != today:
        return True  # primeira vez hoje (ou o PC ligou depois do horário)
    if state.get("done") or state.get("runs", 0) >= max_runs:
        return False
    last = state.get("last_attempt")
    if last is None:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(minutes=retry_minutes)


def pipeline_python() -> tuple[str, int]:
    """Python e flags para rodar main.py.

    No Windows o agendador roda em pythonw (sem janela), mas cada FFmpeg/claude que um
    processo sem console abre ganha uma janela própria piscando na tela. Por isso o
    pipeline roda em python.exe com um console oculto (CREATE_NO_WINDOW), que os
    processos filhos herdam.
    """
    exe = Path(sys.executable)
    if os.name != "nt":
        return str(exe), 0
    console = exe.with_name("python.exe")
    return str(console if console.exists() else exe), subprocess.CREATE_NO_WINDOW


def run_once(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    retry = int(os.getenv("RETRY_MINUTES") or 30)
    max_runs = int(os.getenv("MAX_RUNS_PER_DAY") or 4)
    state = load_state()
    if not should_run(now, run_time(), state, retry, max_runs):
        return state

    today = now.date().isoformat()
    if state.get("day") != today:
        state = {"day": today, "runs": 0, "done": False}
    state["runs"] += 1
    state["last_attempt"] = now.isoformat(timespec="seconds")
    save_state(state)

    log.info("Iniciando vídeo de %s (execução %s/%s)", today, state["runs"], max_runs)
    try:
        python, flags = pipeline_python()
        result = subprocess.run(
            [python, str(PROJECT_DIR / "main.py"), "--date", today],
            cwd=PROJECT_DIR, timeout=RUN_TIMEOUT_SECONDS, creationflags=flags,
        )
        state["done"] = result.returncode == 0
        state["last_exit_code"] = result.returncode
    except subprocess.TimeoutExpired:
        state["last_exit_code"] = "timeout"
        log.error("main.py passou de %sh e foi interrompido", RUN_TIMEOUT_SECONDS // 3600)

    save_state(state)
    if state["done"]:
        log.info("Vídeo de %s pronto. Veja output/ e logs/%s.log", today, today)
    elif state["runs"] >= max_runs:
        log.error("Vídeo de %s falhou %s vezes; veja logs/%s.log. Próxima tentativa amanhã.",
                  today, state["runs"], today)
    else:
        log.warning("Falhou; nova tentativa em %s min (retoma do checkpoint)", retry)
    return state


def acquire_single_instance(port: int = SINGLE_INSTANCE_PORT) -> socket.socket | None:
    """Ocupa uma porta local; se já estiver ocupada, outro agendador está rodando.
    O sistema libera a porta sozinho quando o processo termina (até em crash)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Roda main.py todo dia no horário do .env.")
    parser.add_argument("--once", action="store_true", help="verifica uma vez e sai")
    args = parser.parse_args(argv)

    (PROJECT_DIR / "logs").mkdir(exist_ok=True)
    handlers = [RotatingFileHandler(PROJECT_DIR / "logs" / "scheduler.log",
                                    maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:  # pythonw (Windows, sem janela) não tem console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)

    instance = acquire_single_instance()
    if instance is None:
        log.warning("Outro agendador já está rodando; saindo")
        return 0

    scheduled = run_time()
    log.info("Agendador ativo: vídeo diário às %s", scheduled.strftime("%H:%M"))
    if args.once:
        run_once()
        return 0
    while True:
        try:
            run_once()
        except Exception:  # o agendador nunca morre por causa de uma execução
            log.exception("Erro inesperado no agendador")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
