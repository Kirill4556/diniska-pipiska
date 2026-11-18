# Сборка для Windows: папка packaging\dist\Splitter и, если установлен Inno Setup, установщик.
# Запуск из корня проекта:  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

uv sync
uv run pyinstaller packaging/splitter.spec --noconfirm --distpath packaging/dist --workpath packaging/build --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
Write-Host "Приложение: packaging\dist\Splitter\Splitter.exe"

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($iscc) {
    & $iscc packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
    Write-Host "Установщик: packaging\dist\SplitterSetup-*.exe (вместе с .bin-файлами рядом)"
} else {
    Write-Host "Inno Setup не найден — установщик не собран. Поставить: winget install JRSoftware.InnoSetup"
}
