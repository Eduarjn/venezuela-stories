# Fila de postagem: agenda na Upload-Post os videos novos da pasta, nos proximos horarios livres.
# Os videos ja agendados ficam como estao; a Upload-Post publica na hora marcada mesmo com o PC
# desligado. Precisa de UPLOAD_POST_API_KEY e UPLOAD_POST_USER no .env (veja publicar.py).
#
# Rodar agora (na pasta do projeto):
#   .\agendar_postagens.ps1 -Pasta "C:\Users\eduar\Downloads\PRONTO_PARA_POSTAR"
#
# Rodar sozinho todo dia as 08:00 (registre uma vez, no PowerShell, dentro da pasta do projeto):
#   $script = (Resolve-Path .\agendar_postagens.ps1).Path
#   $acao = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
#   $quando = New-ScheduledTaskTrigger -Daily -At 08:00
#   $config = New-ScheduledTaskSettingsSet -StartWhenAvailable
#   Register-ScheduledTask -TaskName "FatoRaro postagens" -Action $acao -Trigger $quando -Settings $config
# (StartWhenAvailable: se o PC estava desligado as 08:00, roda quando ligar.)
# Para tirar: Unregister-ScheduledTask -TaskName "FatoRaro postagens"

param(
    [string]$Pasta = "$env:USERPROFILE\Downloads\PRONTO_PARA_POSTAR",
    [string[]]$Horarios = @("12:00", "19:00")
)

Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$log = "logs\postagens.log"
Add-Content -Path $log -Value ("=== " + (Get-Date -Format "yyyy-MM-dd HH:mm") + " pasta: " + $Pasta)
if (-not (Test-Path $Pasta)) {
    Add-Content -Path $log -Value "[X] pasta nao encontrada"
    Write-Output "[X] pasta nao encontrada: $Pasta"
    exit 1
}
& .\.venv\Scripts\python.exe publicar.py --pasta $Pasta --horarios $Horarios 2>&1 | Tee-Object -FilePath $log -Append
exit $LASTEXITCODE
