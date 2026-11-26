# web-share: установка / обновление / удаление на Windows 10/11 и Windows Server 2016+.
#
#   start.cmd  (или: powershell -ExecutionPolicy Bypass -File start.ps1)
#                                    спросит пути, порт, логины... и запустит службу (задача планировщика)
#   start.cmd -Yes                   без вопросов: значения по умолчанию / из прошлой установки / из -Answers
#   start.cmd -Answers my.env        заранее заданные ответы (файл KEY=VALUE, ключи как в .env.example)
#   start.cmd -Uninstall             убрать задачу и правило брандмауэра (данные общей папки не трогаются)
#
# Значения по умолчанию: -Answers > .env прошлой установки > .env проекта (или .env.example) > встроенные.
# Повторный запуск = обновление. Итоговые настройки - <папка установки>\.env.
# Python 3.7+ ставится с python.org, если его нет; программа запускается из venv в папке установки.
# Файл в UTF-8 с BOM: иначе Windows PowerShell 5.1 прочтёт русский текст как ANSI.
[CmdletBinding()]
param(
    [switch]$Yes,
    [string]$Answers = "",
    [switch]$Uninstall,
    [switch]$PauseAtEnd
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest в 5.1 с полосой прогресса качает в разы медленнее
$App = "web-share"
$Src = $PSScriptRoot
$PyVersion = "3.12.10"

function Say($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die($m) {
    Write-Host "[X] $m" -ForegroundColor Red
    if ($PauseAtEnd) { Read-Host "Enter - закрыть окно" | Out-Null }
    exit 1
}

# ---------------------------------------------------------------- права администратора
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $a = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-PauseAtEnd")
    if ($Yes) { $a += "-Yes" }
    if ($Uninstall) { $a += "-Uninstall" }
    if ($Answers) { $a += @("-Answers", "`"$((Resolve-Path $Answers).Path)`"") }
    Write-Host "Нужны права администратора - запрашиваю (окно UAC)..."
    $p = Start-Process powershell.exe -Verb RunAs -ArgumentList $a -Wait -PassThru
    exit $p.ExitCode
}

# ---------------------------------------------------------------- ответы
$Interactive = (-not $Yes) -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
$Pre = @{}    # -Answers, затем .env прошлой установки (не перетирают уже заданное)
$Proj = @{}   # .env (или .env.example) рядом со start.ps1; пустые и линуксовые пути пропускаются

function Read-Kv([string]$path, [hashtable]$into, [switch]$SkipEmpty) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    foreach ($line in [IO.File]::ReadAllLines($path, [Text.Encoding]::UTF8)) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line -split '=', 2
        $k = $k.Trim(); $v = $v.Trim()
        if (-not $k -or $into.ContainsKey($k)) { continue }
        if ($SkipEmpty -and -not $v) { continue }
        $into[$k] = $v
    }
}
$projFile = Join-Path $Src ".env"
if (-not (Test-Path $projFile)) { $projFile = Join-Path $Src ".env.example" }
Read-Kv $projFile $Proj -SkipEmpty
# в .env.example - значения для Linux (/srv/share, /opt/...): в Windows от них толку нет
foreach ($k in @($Proj.Keys)) { if ($Proj[$k] -like "/*") { $Proj.Remove($k) } }
$Proj.Remove("INSTALL_RUN_USER")
if ($Proj["SHARE_HIDE"] -eq "lost+found") { $Proj.Remove("SHARE_HIDE") }   # в Windows по умолчанию скрыто ещё и своё
if ($Answers) {
    if (-not (Test-Path -LiteralPath $Answers)) { Die "нет файла $Answers" }
    Read-Kv $Answers $Pre
}

function Def([string]$key, [string]$builtin) {
    if ($Pre.ContainsKey($key)) { return $Pre[$key] }
    if ($Proj.ContainsKey($key)) { return $Proj[$key] }
    return $builtin
}
# Ask КЛЮЧ "вопрос" "по умолчанию": Enter - значение в [скобках], "-" - пусто
function Ask([string]$key, [string]$q, [string]$builtin) {
    $d = Def $key $builtin
    $a = $d
    if ($Interactive) {
        $hint = ""
        if ($d) { $hint = " [$d]" }
        $a = Read-Host "$q$hint"
        if (-not $a) { $a = $d }
        if ($a -eq "-") { $a = "" }
    }
    Set-Variable -Name $key -Value $a -Scope Script
}
function AskYn([string]$key, [string]$q, [string]$builtin) {
    $d = Def $key $builtin
    $a = $d
    if ($Interactive) {
        $hint = "д/Н"
        if ($d -eq "y") { $hint = "Д/н" }
        $r = (Read-Host "$q [$hint]").Trim().ToLower()
        if (@("д", "y", "да", "yes") -contains $r) { $a = "y" }
        elseif (@("н", "n", "нет", "no") -contains $r) { $a = "n" }
    }
    Set-Variable -Name $key -Value $a -Scope Script
}
function Setting([string]$key, [string]$builtin) { Set-Variable -Name $key -Value (Def $key $builtin) -Scope Script }
function New-Password {
    $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $b = New-Object byte[] 12
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $chars[$_ % $chars.Length] })
}
function Host-IPs {
    try {
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            ForEach-Object { $_.IPAddress }
    } catch { @() }
}
# процессы службы: python (и лаунчер venv), запущенный с share-web.py из папки установки
function Stop-App([string]$dir, [string]$task) {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    }
    $script = Join-Path $dir "share-web.py"
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Account([string]$sid) {
    (New-Object Security.Principal.SecurityIdentifier $sid).Translate([Security.Principal.NTAccount]).Value
}

Write-Host "web-share — веб-файловый менеджер общей папки (Windows)" -ForegroundColor White
if ($Interactive) { Write-Host "Enter — значение в [скобках], «-» — оставить пустым." }
Ask INSTALL_DIR "Папка установки" (Join-Path $env:ProgramFiles $App)
$INSTALL_DIR = $INSTALL_DIR.TrimEnd("\")
Read-Kv (Join-Path $INSTALL_DIR ".env") $Pre
Ask INSTALL_SERVICE "Имя службы (задачи планировщика)" $App
$Task = $INSTALL_SERVICE
$FwRule = "$Task-http"

if ($Uninstall) {
    Say "Удаление $Task"
    Stop-App $INSTALL_DIR $Task
    if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Task -Confirm:$false
        Write-Host "задача $Task удалена"
    }
    Get-NetFirewallRule -Name $FwRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    AskYn DEL_DIR "Удалить папку установки $INSTALL_DIR (код, venv, .env, логи, комментарии)?" "y"
    if ($DEL_DIR -eq "y" -and (Test-Path -LiteralPath $INSTALL_DIR)) {
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $INSTALL_DIR -Recurse -Force
    }
    Write-Host "Готово. Общая папка $(Def SHARE_ROOT '') не тронута."
    if ($PauseAtEnd) { Read-Host "Enter - закрыть окно" | Out-Null }
    exit 0
}

Ask SHARE_ROOT "Общая папка (корень)" "C:\Share"
if ($SHARE_ROOT -match '^[A-Za-z]:\\?$') { $SHARE_ROOT = $SHARE_ROOT.Substring(0, 2) + "\" }   # корень диска: D:\
else { $SHARE_ROOT = $SHARE_ROOT.TrimEnd("\") }
Ask SHARE_PORT "Порт веб-интерфейса" "8081"
$portNum = 0
if (-not [int]::TryParse($SHARE_PORT, [ref]$portNum) -or $portNum -lt 1 -or $portNum -gt 65535) { Die "плохой порт: $SHARE_PORT" }
Ask SHARE_BIND "Адрес, на котором слушать (0.0.0.0 — все)" "0.0.0.0"
Ask SHARE_ALLOW "С каких сетей пускать (CIDR через запятую)" "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"
$leaf = Split-Path -Leaf $SHARE_ROOT
if (-not $leaf) { $leaf = $SHARE_ROOT.TrimEnd("\") }
Ask SHARE_TITLE "Название в шапке" $leaf
Ask SHARE_USER "Логин с полным доступом (изменение файлов)" "admin"
Ask SHARE_PASS "Пароль для $SHARE_USER" (New-Password)
Ask SHARE_UPLOAD_USER "Логин «только загрузка» (пусто — не нужен)" "upload"
if ($SHARE_UPLOAD_USER) {
    Ask SHARE_UPLOAD_PASS "Пароль для $SHARE_UPLOAD_USER" (New-Password)
    Ask SHARE_UPLOAD_DIR "Папка для загрузки (от корня общей папки)" "upload"
    $SHARE_UPLOAD_DIR = $SHARE_UPLOAD_DIR.Trim("\", "/")
} else {
    $SHARE_UPLOAD_PASS = ""; $SHARE_UPLOAD_DIR = ""
}
Ask SHARE_SMB_PATHS "Сетевые пути для показа в шапке, например \\сервер\share (через запятую, пусто — не показывать)" ""

Setting SHARE_SESSION_HOURS "12"
Setting SHARE_COMMENTS_FILE ""
Setting SHARE_HIDE "lost+found,`$RECYCLE.BIN,System Volume Information"
Setting SHARE_LOG_LEVEL "INFO"
$SHARE_LOG_LEVEL = $SHARE_LOG_LEVEL.ToUpper()
if (@("ERROR", "WARNING", "INFO", "DEBUG") -notcontains $SHARE_LOG_LEVEL) {
    Warn "SHARE_LOG_LEVEL=$SHARE_LOG_LEVEL - нет такого уровня, будет INFO"; $SHARE_LOG_LEVEL = "INFO"
}
Setting INSTALL_LOG_DIR (Join-Path $INSTALL_DIR "log")
if (-not $INSTALL_LOG_DIR -or $INSTALL_LOG_DIR -eq "journal") { $INSTALL_LOG_DIR = Join-Path $INSTALL_DIR "log" }
$INSTALL_LOG_DIR = $INSTALL_LOG_DIR.TrimEnd("\")
$SHARE_LOG_FILE = Join-Path $INSTALL_LOG_DIR "$Task.log"

# ---------------------------------------------------------------- Python
function Test-Python([string]$exe, [string[]]$pre) {
    $ErrorActionPreference = "Continue"   # в 5.1 вывод в stderr при Stop - исключение
    try {
        $out = & $exe @pre -c "import sys, venv; print(sys.executable) if sys.version_info >= (3, 7) else sys.exit(1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
    } catch { }
    return $null
}
function Find-Python {
    # сначала - установленный для всех (служба работает от SYSTEM), затем py и python из PATH
    $dirs = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base -and (Test-Path $base)) {
            $dirs += Get-ChildItem -Path $base -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
                Sort-Object { [int]($_.Name -replace '\D', '') } -Descending
        }
    }
    foreach ($d in $dirs) {
        $p = Test-Python (Join-Path $d.FullName "python.exe") @()
        if ($p) { return $p }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = Test-Python "py" @("-3")
        if ($p) { return $p }
    }
    foreach ($n in "python", "python3") {
        $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        # python.exe из WindowsApps - заглушка, открывающая Microsoft Store
        if ($c -and $c.Source -notlike "*\WindowsApps\*") {
            $p = Test-Python $c.Source @()
            if ($p) { return $p }
        }
    }
    return $null
}

Say "Python"
$Py = Find-Python
if (-not $Py) {
    $arch = "amd64"
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { $arch = "arm64" }
    $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-$arch.exe"
    $exe = Join-Path $env:TEMP "python-$PyVersion-$arch.exe"
    Write-Host "Python 3.7+ не найден - ставлю $PyVersion для всех пользователей ($url)"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $exe
    $sig = Get-AuthenticodeSignature -FilePath $exe
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        Die "подпись установщика Python не прошла проверку ($($sig.Status))"
    }
    $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=1", "PrependPath=0", "Include_test=0", "Include_doc=0", "Include_tcltk=0", "Shortcuts=0")
    Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { Die "установщик Python завершился с кодом $($p.ExitCode)" }
    $Py = Find-Python
    if (-not $Py) { Die "Python поставлен, но не найден" }
}
Write-Host "Python: $Py ($(& $Py -V 2>&1))"

# ---------------------------------------------------------------- файлы
Say "Общая папка $SHARE_ROOT"
foreach ($d in @($SHARE_ROOT) + @($(if ($SHARE_UPLOAD_DIR) { Join-Path $SHARE_ROOT $SHARE_UPLOAD_DIR }))) {
    if ($d -and -not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-Host "создана $d"
    }
}

Say "Файлы в $INSTALL_DIR"
Stop-App $INSTALL_DIR $Task
New-Item -ItemType Directory -Path $INSTALL_DIR -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Src "share-web.py") -Destination $INSTALL_DIR -Force
$VPy = Join-Path $INSTALL_DIR "venv\Scripts\python.exe"
$venvOk = $false
if (Test-Path -LiteralPath $VPy) {
    & { $ErrorActionPreference = "Continue"; & $VPy -c "import sys" 2>$null }
    $venvOk = $LASTEXITCODE -eq 0
}
if (-not $venvOk) {
    $venv = Join-Path $INSTALL_DIR "venv"
    if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
    & { $ErrorActionPreference = "Continue"; & $Py -m venv $venv }
    if ($LASTEXITCODE -ne 0) { Die "не удалось создать venv в $venv" }
    Write-Host "создано виртуальное окружение $venv ($(& $VPy -V 2>&1))"
}

$cfg = Join-Path $INSTALL_DIR ".env"
$lines = @("# ${App}: настройки (создано start.ps1 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')). После правки: start.cmd -Yes")
foreach ($k in "SHARE_ROOT", "SHARE_PORT", "SHARE_BIND", "SHARE_ALLOW", "SHARE_TITLE", "SHARE_SMB_PATHS", "SHARE_USER",
    "SHARE_PASS", "SHARE_UPLOAD_USER", "SHARE_UPLOAD_PASS", "SHARE_UPLOAD_DIR", "SHARE_SESSION_HOURS",
    "SHARE_COMMENTS_FILE", "SHARE_HIDE", "SHARE_LOG_LEVEL", "SHARE_LOG_FILE") {
    $lines += "$k=$((Get-Variable -Name $k -Scope Script).Value)"
}
$lines += "# для start.ps1 (приложение не читает)"
foreach ($k in "INSTALL_SERVICE", "INSTALL_LOG_DIR") { $lines += "$k=$((Get-Variable -Name $k -Scope Script).Value)" }
[IO.File]::WriteAllLines($cfg, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
# пароли: читать .env могут только SYSTEM и администраторы (SID - имена групп зависят от языка Windows)
& icacls.exe $cfg /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
Write-Host "настройки: $cfg"
New-Item -ItemType Directory -Path $INSTALL_LOG_DIR -Force | Out-Null
Write-Host "лог: $SHARE_LOG_FILE (ротация: 20 МБ, хранится 8)"

# ---------------------------------------------------------------- служба
Say "Служба $Task (планировщик заданий, от SYSTEM)"
$action = New-ScheduledTaskAction -Execute $VPy -Argument "-I `"$(Join-Path $INSTALL_DIR 'share-web.py')`"" `
    -WorkingDirectory $INSTALL_DIR
# при загрузке + каждые 5 минут: если процесс упал - поднимется снова, если работает - IgnoreNew
$boot = New-ScheduledTaskTrigger -AtStartup
$again = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId (Account "S-1-5-18") -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $Task -Action $action -Trigger @($boot, $again) -Principal $principal `
    -Settings $settings -Description "${App}: файловый менеджер для $SHARE_ROOT (порт $SHARE_PORT). Настройки - $cfg" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $Task

Get-NetFirewallRule -Name $FwRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $FwRule -DisplayName "$App (TCP $SHARE_PORT)" -Direction Inbound -Protocol TCP `
    -LocalPort $SHARE_PORT -Action Allow -Profile Any | Out-Null
Write-Host "брандмауэр: разрешён входящий TCP $SHARE_PORT (правило $FwRule)"

# ---------------------------------------------------------------- проверка
Say "Проверка"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$SHARE_PORT/api/version" | Out-Null
        $ok = $true; break
    } catch {
        # ответ с ошибкой (403, если 127.0.0.1 не в SHARE_ALLOW) - сервер всё равно работает
        if ($_.Exception.Response) { $ok = $true; break }
        Start-Sleep -Seconds 1
    }
}
if (-not $ok) {
    if (Test-Path -LiteralPath $SHARE_LOG_FILE) { Get-Content -LiteralPath $SHARE_LOG_FILE -Tail 15 }
    Die "$Task не отвечает на порту $SHARE_PORT"
}
Write-Host "$Task работает (python из $INSTALL_DIR\venv)" -ForegroundColor Green
Write-Host ""
Write-Host "Адреса:"
foreach ($a in Host-IPs) { Write-Host "  http://${a}:$SHARE_PORT/" }
Write-Host "Полный доступ:   $SHARE_USER / $SHARE_PASS"
if ($SHARE_UPLOAD_USER) { Write-Host "Только загрузка: $SHARE_UPLOAD_USER / $SHARE_UPLOAD_PASS (в /$SHARE_UPLOAD_DIR)" }
Write-Host "Настройки: $cfg   Лог: $SHARE_LOG_FILE"
if ($PauseAtEnd) { Read-Host "Enter - закрыть окно" | Out-Null }
