#!/usr/bin/env bash
# Inicia o agendador em segundo plano (macOS/Linux).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Ambiente não encontrado. Rode antes: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
mkdir -p logs
nohup .venv/bin/python daily_scheduler.py >> logs/scheduler.out 2>&1 &
echo "Agendador iniciado (PID $!). Log: logs/scheduler.log"
