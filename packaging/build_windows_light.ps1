# Лёгкая сборка для Windows (FR-32): PyTorch без CUDA — для компьютеров без подходящей видеокарты NVIDIA.
# Та же версия PyTorch, что и в основной сборке, но из индекса CPU: установщик в разы меньше.
# Отдельное окружение packaging\.venv-light, основная сборка не меняется.
# Запуск из корня проекта:  powershell -ExecutionPolicy Bypass -File packaging\build_windows_light.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$venv = "packaging\.venv-light"
$python = "$venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    uv venv $venv --python 3.11
    if ($LASTEXITCODE -ne 0) { throw "Не удалось создать окружение" }
}
# версия torch — как в основной сборке (uv.lock), только без CUDA
$torch = (uv run --no-sync python -c "import torch; print(torch.__version__.split('+')[0])").Trim()
uv pip install --python $python "torch==$torch" --index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить PyTorch без CUDA" }
# остальное — как в проекте; --no-sources: не брать torch из CUDA-индекса, установленный уже подходит
uv pip install --python $python --no-sources -e . pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости" }
& $python -c "import torch; assert not torch.version.cuda, 'в лёгкой сборке оказался PyTorch с CUDA'; print('torch', torch.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Неверная сборка PyTorch" }

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
