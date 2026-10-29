@echo off
REM Inicia o agendador em segundo plano (sem janela).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto run
echo Ambiente nao encontrado. Rode antes: python -m venv .venv  e  .venv\Scripts\pip install -r requirements.txt
pause
exit /b 1
:run
start "" ".venv\Scripts\pythonw.exe" daily_scheduler.py
echo Agendador iniciado. Log: logs\scheduler.log
