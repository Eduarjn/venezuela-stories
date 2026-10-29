#!/usr/bin/env bash
# Faz o agendador iniciar sozinho quando o computador liga (crontab @reboot).
DIR="$(cd "$(dirname "$0")" && pwd)"
LINE="@reboot sleep 60 && \"$DIR/start_scheduler.sh\""
( crontab -l 2>/dev/null | grep -vF "$DIR/start_scheduler.sh"; echo "$LINE" ) | crontab -
echo "Instalado no crontab: $LINE"
echo "Para desinstalar: crontab -e e apague a linha."
