# Сборка ModUp.exe (один файл): .\build.ps1  → dist\ModUp.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv; .\.venv\Scripts\python -m pip install pywebview pyinstaller }
.\.venv\Scripts\python tests_modup.py
if ($LASTEXITCODE -ne 0) { throw "Тесты не прошли" }
# PyInstaller пишет обычные сообщения в stderr — при Stop PowerShell принял бы их за ошибку.
$ErrorActionPreference = "Continue"
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name ModUp --icon modup.ico --version-file version_info.txt `
  --add-data "ui;ui" main.py 2>&1 | Out-File build.log -Encoding utf8
if ($LASTEXITCODE -ne 0) { Get-Content build.log -Tail 30; throw "Сборка не удалась (build.log)" }
Write-Host "Готово: dist\ModUp.exe"
