# Лёгкая сборка для Windows (FR-32): без CUDA, с DirectML (FR-31) — для компьютеров без подходящей NVIDIA.
# PyTorch 2.4.1 без CUDA + torch-directml: ускорение на видеокартах AMD, Intel и NVIDIA через DirectX 12,
# без видеокарты — расчёт на процессоре. Отдельное окружение, основная сборка не меняется.
# Запуск из корня проекта:  powershell -ExecutionPolicy Bypass -File packaging\build_windows_light.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$venv = "packaging\.venv-light"
$python = "$venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    uv venv $venv --python 3.11
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать окружение" }
}
# torch-directml требует ровно torch 2.4.1; на PyPI для Windows это сборка без CUDA
uv pip install --python $python --no-sources --override packaging\light-overrides.txt -e . pyinstaller "torch-directml==0.2.5.dev240914"
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости" }

& $python -m PyInstaller packaging/splitter.spec --noconfirm --distpath packaging/dist-light --workpath packaging/build-light --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }
Write-Host "Приложение: packaging\dist-light\Splitter\Splitter.exe"

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($iscc) {
    & $iscc /DLight packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
    Write-Host "Установщик: packaging\dist\SplitterSetup-light-*.exe"
} else {
    Write-Host "Inno Setup не найден — установщик не собран. Поставить: winget install JRSoftware.InnoSetup"
}
