@echo off
REM Faz o agendador iniciar sozinho quando voce entra no Windows.
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\venezuela-stories.bat"
echo @echo off> "%TARGET%"
echo call "%~dp0start_scheduler.bat">> "%TARGET%"
echo Instalado: %TARGET%
echo Para desinstalar, apague esse arquivo.
pause
