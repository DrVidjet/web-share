# web-share: установка на Windows одной командой, без git и ручного скачивания.
# В PowerShell (обычном - права администратора запросятся сами):
#
#   irm https://raw.githubusercontent.com/DrVidjet/web-share/HEAD/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/DrVidjet/web-share/HEAD/install.ps1))) -Yes
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/DrVidjet/web-share/HEAD/install.ps1))) -Uninstall
#
# Скачивает проект (zip с GitHub) во временную папку и запускает из него start.ps1 с теми же параметрами.
# Повторный запуск = обновление. WEBSHARE_REPO / WEBSHARE_REF - другой репозиторий / ветка,
# WEBSHARE_SRC - взять проект из локальной папки (для CI).
# Файл в UTF-8 без BOM: его читает irm | iex (iex не любит BOM); start.ps1 - с BOM.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repo = if ($env:WEBSHARE_REPO) { $env:WEBSHARE_REPO } else { "DrVidjet/web-share" }
$ref = if ($env:WEBSHARE_REF) { $env:WEBSHARE_REF } else { "HEAD" }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("web-share-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    if ($env:WEBSHARE_SRC) {
        $src = $env:WEBSHARE_SRC
    } else {
        Write-Host "Скачиваю $repo ($ref)..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $zip = Join-Path $tmp "src.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$repo/archive/$ref.zip" -OutFile $zip
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp
        $src = (Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1).FullName
    }
    $start = Join-Path $src "start.ps1"
    if (-not (Test-Path -LiteralPath $start)) { throw "нет $start" }
    # отдельный процесс: start.ps1 сам попросит права администратора, а exit в нём не закроет это окно
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $start @args
    if ($LASTEXITCODE) { throw "start.ps1 завершился с кодом $LASTEXITCODE" }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
