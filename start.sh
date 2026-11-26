#!/bin/bash
# web-share: установка / обновление / удаление на Linux с systemd
# (Debian 10-13 / Ubuntu / Astra, Fedora / RHEL, Arch, openSUSE). Windows - start.ps1.
#
#   sudo ./start.sh                  спросит пути, порт, логины... и запустит службу (systemd + cron)
#   sudo ./start.sh -y               без вопросов: значения по умолчанию / из прошлой установки / из --answers
#   sudo ./start.sh --answers F      заранее заданные ответы (файл KEY=VALUE, ключи как в .env.example)
#   sudo ./start.sh --uninstall      убрать службу, cron, Samba-конфиг (данные общей папки не трогаются)
#
# Значения по умолчанию: --answers > .env прошлой установки > .env проекта (или .env.example) > встроенные.
# Повторный запуск = обновление. Итоговые настройки - <папка установки>/.env.
# Python запускается только из виртуального окружения <папка установки>/venv.
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
APP=web-share

# ---------------------------------------------------------------- общее
if [ "$(id -u)" != 0 ]; then
  command -v sudo >/dev/null && exec sudo bash "$0" "$@"
  echo "Нужен root: su -c 'bash $0 $*'" >&2; exit 1
fi
YES=0; ANSWERS=""; UNINSTALL=0
while [ $# -gt 0 ]; do
  case $1 in
    -y|--yes) YES=1 ;;
    --answers) ANSWERS=$2; shift ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 1 ;;
  esac
  shift
done

c_g=$'\e[1;32m'; c_y=$'\e[1;33m'; c_r=$'\e[1;31m'; c_b=$'\e[1m'; c_0=$'\e[0m'
say()  { printf '\n%s==> %s%s\n' "$c_g" "$*" "$c_0"; }
warn() { printf '%s[!] %s%s\n' "$c_y" "$*" "$c_0" >&2; }
die()  { printf '%s[X] %s%s\n' "$c_r" "$*" "$c_0" >&2; exit 1; }

declare -A PRE=()   # заранее заданные ответы: --answers, затем .env прошлой установки
declare -A PROJ=()  # значения по умолчанию из .env (или .env.example) рядом со start.sh; пустые пропускаются
read_kv() {         # read_kv файл -> PRE[...] (не перетирает уже заданное)
  local k v
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    [[ $line =~ ^[[:space:]]*# || $line != *=* ]] && continue
    k=${line%%=*}; v=${line#*=}; k=${k//[[:space:]]/}
    [ -n "$k" ] && [ -z "${PRE[$k]+x}" ] && PRE[$k]=$v
  done < "$1"
  return 0  # иначе set -e роняет скрипт, если ключ последней строки уже задан
}
read_proj() {
  local f=$SRC/.env k v
  [ -f "$f" ] || f=$SRC/.env.example
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    [[ $line =~ ^[[:space:]]*# || $line != *=* ]] && continue
    k=${line%%=*}; v=${line#*=}; k=${k//[[:space:]]/}
    [ -n "$k" ] && [ -n "$v" ] && PROJ[$k]=$v
  done < "$f"
  return 0
}
read_proj
# setting ПЕРЕМЕННАЯ "по умолчанию" - без вопроса (редкие настройки: только из --answers / .env)
setting() { printf -v "$1" '%s' "${PRE[$1]-${PROJ[$1]-$2}}"; }
# ask ПЕРЕМЕННАЯ "вопрос" "по умолчанию"
ask() {
  local var=$1 q=$2 def=${PRE[$1]-${PROJ[$1]-$3}} a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then a=$def
  else
    read -r -p "$q${def:+ [$def]}: " a || true
    [ -z "$a" ] && a=$def
    [ "$a" = "-" ] && a=""
  fi
  printf -v "$var" '%s' "$a"
}
# ask_yn ПЕРЕМЕННАЯ "вопрос" y|n  -> y|n
ask_yn() {
  local var=$1 q=$2 def=${PRE[$1]-${PROJ[$1]-$3}} a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then a=$def
  else
    read -r -p "$q [$( [ "$def" = y ] && echo Д/н || echo д/Н )]: " a || true
    case ${a,,} in д|y|да|yes) a=y ;; н|n|нет|no) a=n ;; *) a=$def ;; esac
  fi
  printf -v "$var" '%s' "$a"
}
gen_pass() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12 || true; }

have() { command -v "$1" >/dev/null 2>&1; }
export PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"   # su без «-» не даёт sbin: useradd, smbd, logrotate

# дистрибутив и пакетный менеджер
PM=""
check_os() {
  # shellcheck disable=SC1091
  [ -f /etc/os-release ] && . /etc/os-release
  if have apt-get; then PM=apt
  elif have dnf; then PM=dnf
  elif have yum; then PM=yum
  elif have zypper; then PM=zypper
  elif have pacman; then PM=pacman
  else warn "неизвестный пакетный менеджер (${PRETTY_NAME:-?}): нужные программы ставьте сами (см. README)"; fi
  if [ "$PM" = apt ] && [ -f /etc/debian_version ]; then
    local dv; dv=$(cut -d. -f1 /etc/debian_version 2>/dev/null)   # у производных (Astra и т.п.) - версия базы Debian
    case $dv in 10|11|12|13) [ "${ID:-}" = debian ] || echo "${PRETTY_NAME:-?} (база Debian $dv)" ;;
      *) [ "${ID:-}" = debian ] && warn "Проверено на Debian 10-13, тут ${PRETTY_NAME:-?} - пробую" ;; esac
  fi
  [ "$PM" = apt ] || echo "${PRETTY_NAME:-Linux} ($PM)"
}
PM_UPDATED=0
apt_update() {
  [ "$PM_UPDATED" = 1 ] && return 0
  if ! apt-get update -q; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${VERSION_CODENAME:-}" = buster ] || [ "${VERSION_CODENAME:-}" = stretch ]; then
      warn "$VERSION_CODENAME больше не на основных зеркалах - переключаю apt на archive.debian.org"
      cp -a /etc/apt/sources.list "/etc/apt/sources.list.bak-$(date +%s)" 2>/dev/null || true
      cat > /etc/apt/sources.list <<EOF
deb http://archive.debian.org/debian $VERSION_CODENAME main contrib non-free
deb http://archive.debian.org/debian-security $VERSION_CODENAME/updates main contrib non-free
EOF
      rm -f /etc/apt/sources.list.d/debian.sources
      echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive
      apt-get update -q || die "apt-get update не прошёл"
    else
      die "apt-get update не прошёл - проверьте сеть и /etc/apt/sources.list"
    fi
  fi
  PM_UPDATED=1
}
# есть ли уже то, что даёт логический пакет
has_feature() {
  case $1 in
    python)    have python3 && python3 -c 'import venv, ensurepip' 2>/dev/null ;;
    certs)     [ -e /etc/ssl/certs/ca-certificates.crt ] || [ -e /etc/pki/tls/certs/ca-bundle.crt ] || [ -d /etc/ssl/certs ] ;;
    useradd)   have useradd && have runuser ;;
    tools)     have find && have flock && have tar && have gzip ;;
    cron)      have crond || have cron ;;
    logrotate) have logrotate ;;
    samba)     have smbd && have smbpasswd && have testparm ;;
    *)         return 1 ;;
  esac
}
# логический пакет -> имена пакетов дистрибутива
pkg_names() {
  case $PM:$1 in
    apt:python) echo python3 python3-venv ;;         *:python) [ "$PM" = pacman ] && echo python || echo python3 ;;
    *:certs) echo ca-certificates ;;
    apt:useradd) echo passwd util-linux ;;  pacman:useradd|zypper:useradd) echo shadow util-linux ;;  *:useradd) echo shadow-utils util-linux ;;
    *:tools) echo findutils util-linux tar gzip ;;
    apt:cron|zypper:cron) echo cron ;;   *:cron) echo cronie ;;
    *:logrotate) echo logrotate ;;
    *:samba) echo samba ;;
  esac
}
# need логический_пакет... - поставить то, чего не хватает
need() {
  local f miss=() pk=()
  for f in "$@"; do has_feature "$f" || miss+=("$f"); done
  [ ${#miss[@]} = 0 ] && return 0
  [ -n "$PM" ] || die "не хватает: ${miss[*]} - установите вручную"
  for f in "${miss[@]}"; do read -ra n <<< "$(pkg_names "$f")"; pk+=("${n[@]}"); done
  say "Ставлю пакеты: ${pk[*]}"
  case $PM in
    apt)    apt_update; DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${pk[@]}" ;;
    dnf)    dnf install -y -q "${pk[@]}" ;;
    yum)    yum install -y -q "${pk[@]}" ;;
    zypper) zypper -n -q install "${pk[@]}" ;;
    pacman) if [ "$PM_UPDATED" = 0 ]; then pacman -Sy --noconfirm >/dev/null; PM_UPDATED=1; fi
            pacman -S --noconfirm --needed "${pk[@]}" ;;
  esac
  for f in "${miss[@]}"; do has_feature "$f" || die "после установки пакетов всё ещё нет: $f"; done
}
# apt: пакет есть в репозитории - поставить (samba-vfs-modules есть не везде)
apt_optional() { [ "$PM" = apt ] && apt-cache show "$1" >/dev/null 2>&1 && { apt_update; DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$1"; }; }
SYSTEMD=0
[ -d /run/systemd/system ] && SYSTEMD=1   # в контейнере без systemd служба только записывается
sctl() { [ "$SYSTEMD" = 1 ] || return 0; systemctl "$@"; }
# первая существующая служба из списка: unit_of smbd smb -> smbd
unit_of() { local u; for u in "$@"; do [ -n "$(systemctl list-unit-files "$u.service" --no-legend 2>/dev/null)" ] && { echo "$u"; return; }; done; echo "$1"; }
make_venv() {   # make_venv папка
  local v=$1/venv
  if [ -x "$v/bin/python3" ] && "$v/bin/python3" -c 'import sys' 2>/dev/null; then return 0; fi
  rm -rf "$v"
  python3 -m venv "$v" || die "не удалось создать venv в $v"
  echo "создано виртуальное окружение $v ($("$v/bin/python3" -V 2>&1))"
}
host_ips() {
  if have ip; then ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  else hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' || true; fi
}
worm_so() { find /usr/lib /usr/lib64 -path '*samba/vfs/worm.so' 2>/dev/null | grep -q .; }
# открыть порт в ufw / firewalld, если они включены
open_port() {  # open_port 8081/tcp [имя службы firewalld]
  if have ufw && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$1" >/dev/null && echo "ufw: открыт $1"
  elif have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
    if [ -n "${2:-}" ]; then firewall-cmd -q --permanent --add-service="$2"; else firewall-cmd -q --permanent --add-port="$1"; fi
    firewall-cmd -q --reload && echo "firewalld: открыт ${2:-$1}"
  fi
  return 0
}
set_shebang() { sed -i "1s|.*|#!$2 -I|" "$1"; }  # set_shebang файл python

# ---------------------------------------------------------------- ответы
check_os
[ -n "$ANSWERS" ] && { [ -f "$ANSWERS" ] || die "нет файла $ANSWERS"; read_kv "$ANSWERS"; }

echo "${c_b}web-share — веб-файловый менеджер общей папки${c_0}"
[ "$YES" = 1 ] || echo "Enter — значение в [скобках], «-» — оставить пустым."
ask INSTALL_DIR "Папка установки" "/opt/$APP"
read_kv "$INSTALL_DIR/.env"
read_kv "$INSTALL_DIR/config.env"   # старое имя файла настроек
ask INSTALL_SERVICE "Имя службы systemd" "$APP"
SERVICE=$INSTALL_SERVICE
UNIT=/etc/systemd/system/$SERVICE.service
CRON=/etc/cron.d/$SERVICE-images-index
SMBINC=/etc/samba/$SERVICE.conf
LOGROT=/etc/logrotate.d/$SERVICE

if [ "$UNINSTALL" = 1 ]; then
  say "Удаление $SERVICE"
  sctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT" "$CRON" "$LOGROT"
  if [ -f "$SMBINC" ]; then
    rm -f "$SMBINC"; sed -i "\|^[[:space:]]*include = $SMBINC\$|d" /etc/samba/smb.conf
    sctl reload "$(unit_of smbd smb)" 2>/dev/null || true
  fi
  sctl daemon-reload || true
  ask_yn DEL_DIR "Удалить папку установки $INSTALL_DIR (код, venv, .env, логи)?" y
  [ "$DEL_DIR" = y ] && rm -rf "$INSTALL_DIR"
  if [ -d "/var/lib/$SERVICE" ]; then
    ask_yn DEL_STATE "Удалить комментарии к файлам и кэш (/var/lib/$SERVICE)?" y
    [ "$DEL_STATE" = y ] && rm -rf "/var/lib/$SERVICE"
  fi
  echo "Готово. Общая папка ${PRE[SHARE_ROOT]:-} и пользователи не тронуты."
  exit 0
fi

ask SHARE_ROOT "Общая папка (корень)" "/srv/share"
SHARE_ROOT=${SHARE_ROOT%/}
ask SHARE_PORT "Порт веб-интерфейса" "8081"
# shellcheck disable=SC2153  # SHARE_PORT задаёт ask
[[ $SHARE_PORT =~ ^[0-9]+$ ]] && [ "$SHARE_PORT" -ge 1 ] && [ "$SHARE_PORT" -le 65535 ] || die "плохой порт: $SHARE_PORT"
ask SHARE_BIND "Адрес, на котором слушать (0.0.0.0 — все)" "0.0.0.0"
ask SHARE_ALLOW "С каких сетей пускать (CIDR через запятую)" "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"
ask SHARE_TITLE "Название в шапке" "$(basename "$SHARE_ROOT")"
ask INSTALL_RUN_USER "Пользователь, от которого работает служба (владелец файлов)" "webshare"
ask SHARE_USER "Логин с полным доступом (изменение файлов)" "admin"
ask SHARE_PASS "Пароль для $SHARE_USER" "$(gen_pass)"
ask SHARE_UPLOAD_USER "Логин «только загрузка» (пусто — не нужен)" "upload"
if [ -n "$SHARE_UPLOAD_USER" ]; then
  ask SHARE_UPLOAD_PASS "Пароль для $SHARE_UPLOAD_USER" "$(gen_pass)"
  ask SHARE_UPLOAD_DIR "Папка для загрузки (от корня общей папки)" "upload"
  SHARE_UPLOAD_DIR=${SHARE_UPLOAD_DIR#/}; SHARE_UPLOAD_DIR=${SHARE_UPLOAD_DIR%/}
else
  SHARE_UPLOAD_PASS=""; SHARE_UPLOAD_DIR=""
fi
ask INSTALL_DOCKER_INDEX_DIR "Папка с docker-образами для индекса images.list, от корня общей папки (пусто — не нужно)" ""
INSTALL_DOCKER_INDEX_DIR=${INSTALL_DOCKER_INDEX_DIR%/}
# относительный путь - от SHARE_ROOT (точка монтирования задаётся только в SHARE_ROOT); абсолютный - как есть
DOCKER_DIR=""
if [ -n "$INSTALL_DOCKER_INDEX_DIR" ]; then
  case "$INSTALL_DOCKER_INDEX_DIR" in /*) DOCKER_DIR=$INSTALL_DOCKER_INDEX_DIR;; *) DOCKER_DIR=$SHARE_ROOT/$INSTALL_DOCKER_INDEX_DIR;; esac
fi
ask_yn INSTALL_SAMBA "Настроить Samba-шару для этой папки (гость — чтение, $SHARE_USER — запись)?" n
if [ "$INSTALL_SAMBA" = y ]; then
  ask INSTALL_SMB_NAME "Имя Samba-шары" "$(basename "$SHARE_ROOT" | tr -c 'A-Za-z0-9_\n-' _)"
fi
SMB_DEF=""
if [ "$INSTALL_SAMBA" = y ]; then
  for a in $(host_ips); do SMB_DEF+="${SMB_DEF:+,}\\\\$a\\$INSTALL_SMB_NAME"; done
fi
ask SHARE_SMB_PATHS "Пути Samba для показа в шапке (через запятую, пусто — не показывать)" "$SMB_DEF"

setting SHARE_SESSION_HOURS 12
setting SHARE_COMMENTS_FILE ""
setting SHARE_HIDE "lost+found"
setting SHARE_LOG_LEVEL INFO
SHARE_LOG_LEVEL=${SHARE_LOG_LEVEL^^}
case $SHARE_LOG_LEVEL in ERROR|WARNING|INFO|DEBUG) ;; *) warn "SHARE_LOG_LEVEL=$SHARE_LOG_LEVEL - нет такого уровня, будет INFO"; SHARE_LOG_LEVEL=INFO ;; esac
setting INSTALL_SMB_WORM_GRACE 30
# логи: папка (по умолчанию <папка установки>/log) или journal - только журнал systemd
setting INSTALL_LOG_DIR "$INSTALL_DIR/log"
INSTALL_LOG_DIR=${INSTALL_LOG_DIR%/}
[ -z "$INSTALL_LOG_DIR" ] && INSTALL_LOG_DIR=journal
RUN_USER=$INSTALL_RUN_USER
NOLOGIN=$(command -v nologin 2>/dev/null || echo /sbin/nologin)
# ---------------------------------------------------------------- установка
need python certs useradd
[ -n "$INSTALL_DOCKER_INDEX_DIR" ] && need cron tools
[ "$INSTALL_LOG_DIR" != journal ] && need logrotate
[ "$INSTALL_SAMBA" = y ] && need samba

say "Пользователь $RUN_USER и общая папка $SHARE_ROOT"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  useradd -r -M -U -d /nonexistent -s "$NOLOGIN" "$RUN_USER"
  echo "создан системный пользователь $RUN_USER"
fi
RUN_GROUP=$(id -gn "$RUN_USER")
if [ ! -d "$SHARE_ROOT" ]; then
  install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 2775 "$SHARE_ROOT"
  echo "создана $SHARE_ROOT"
elif ! runuser -u "$RUN_USER" -- test -w "$SHARE_ROOT"; then
  ask_yn INSTALL_CHOWN "У $RUN_USER нет прав на запись в $SHARE_ROOT. Сделать его владельцем (рекурсивно)?" n
  if [ "$INSTALL_CHOWN" = y ]; then chown -R "$RUN_USER:$RUN_GROUP" "$SHARE_ROOT"; chmod 2775 "$SHARE_ROOT"
  else warn "$RUN_USER не сможет записывать в $SHARE_ROOT - загрузка и изменения работать не будут"; fi
fi
for d in ${SHARE_UPLOAD_DIR:+"$SHARE_ROOT/$SHARE_UPLOAD_DIR"} ${DOCKER_DIR:+"$DOCKER_DIR"}; do
  [ -d "$d" ] && continue
  install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 2775 "$d"
  echo "создана $d"
done

say "Файлы в $INSTALL_DIR"
install -d -m 755 "$INSTALL_DIR"
install -m 755 "$SRC/share-web.py" "$SRC/share-images-index.sh" "$INSTALL_DIR/"
make_venv "$INSTALL_DIR"
set_shebang "$INSTALL_DIR/share-web.py" "$INSTALL_DIR/venv/bin/python3"
cfg=$INSTALL_DIR/.env
umask 077
{
  echo "# $APP: настройки (создано start.sh $(date '+%F %T')). После правки: systemctl restart $SERVICE"
  for k in SHARE_ROOT SHARE_PORT SHARE_BIND SHARE_ALLOW SHARE_TITLE SHARE_SMB_PATHS SHARE_USER SHARE_PASS \
           SHARE_UPLOAD_USER SHARE_UPLOAD_PASS SHARE_UPLOAD_DIR SHARE_SESSION_HOURS SHARE_COMMENTS_FILE SHARE_HIDE SHARE_LOG_LEVEL; do echo "$k=${!k}"; done
  echo "# для start.sh (приложение не читает)"
  for k in INSTALL_SERVICE INSTALL_RUN_USER INSTALL_DOCKER_INDEX_DIR INSTALL_SAMBA INSTALL_SMB_NAME INSTALL_SMB_WORM_GRACE \
           INSTALL_LOG_DIR; do echo "$k=${!k-}"; done
} > "$cfg.tmp"
umask 022
chown "root:$RUN_GROUP" "$cfg.tmp"; chmod 640 "$cfg.tmp"; mv "$cfg.tmp" "$cfg"
rm -f "$INSTALL_DIR/config.env"
echo "настройки: $cfg"

# логи: вывод службы и индекса docker-образов - в файлы $INSTALL_LOG_DIR, ротация - logrotate (copytruncate)
LOG_UNIT=""; CRON_LOG=""
if [ "$INSTALL_LOG_DIR" != journal ]; then
  # группа службы может писать в папку: cron индекса работает от $RUN_USER и сам создаёт свой файл
  install -d -o root -g "$RUN_GROUP" -m 775 "$INSTALL_LOG_DIR"
  LOG_UNIT=$(printf 'Environment=PYTHONUNBUFFERED=1\nStandardOutput=append:%s/%s.log\nStandardError=inherit' "$INSTALL_LOG_DIR" "$SERVICE")
  [ -n "$INSTALL_DOCKER_INDEX_DIR" ] && CRON_LOG=" >> $INSTALL_LOG_DIR/$SERVICE-images-index.log 2>&1"
  cat > "$LOGROT" <<ROT_END
# $APP (создано start.sh)
$INSTALL_LOG_DIR/*.log {
    weekly
    maxsize 20M
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root $RUN_GROUP
}
ROT_END
  chmod 644 "$LOGROT"
  # Fedora, Arch, openSUSE: logrotate запускает таймер systemd (в Debian - тоже он или cron.daily)
  sctl enable --now logrotate.timer >/dev/null 2>&1 || true
  echo "логи: $INSTALL_LOG_DIR (ротация - $LOGROT)"
else
  rm -f "$LOGROT"
fi

say "Служба $SERVICE"
cat > "$UNIT" <<EOF
# $APP (создано start.sh). Настройки - $cfg
[Unit]
Description=$APP: file manager for $SHARE_ROOT (port $SHARE_PORT)
After=network-online.target local-fs.target
Wants=network-online.target
RequiresMountsFor=$SHARE_ROOT

[Service]
ExecStart=$INSTALL_DIR/venv/bin/python3 -I $INSTALL_DIR/share-web.py
WorkingDirectory=$INSTALL_DIR
User=$RUN_USER
Group=$RUN_GROUP
UMask=0002
# /var/lib/$SERVICE - комментарии к файлам (comments.json), если SHARE_COMMENTS_FILE не задан; кэш CRC для zip (zip-crc.json)
StateDirectory=$SERVICE
StateDirectoryMode=0750
Restart=on-failure
RestartSec=5
$LOG_UNIT

[Install]
WantedBy=multi-user.target
EOF
if [ "$SYSTEMD" = 1 ]; then
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
  open_port "$SHARE_PORT/tcp"
else
  warn "systemd не запущен (контейнер?): служба записана в $UNIT, но не запущена"
fi

if [ -n "$INSTALL_DOCKER_INDEX_DIR" ]; then
  echo "*/5 * * * * $RUN_USER $INSTALL_DIR/share-images-index.sh $DOCKER_DIR$CRON_LOG" > "$CRON"
  chmod 644 "$CRON"
  sctl enable --now "$(unit_of cron crond cronie)" >/dev/null 2>&1 || true
  echo "cron: $CRON (индекс $DOCKER_DIR/images.list раз в 5 минут)"
else
  rm -f "$CRON"
fi

# ---------------------------------------------------------------- Samba
if [ "$INSTALL_SAMBA" = y ]; then
  say "Samba"
  smb_user() {  # smb_user логин пароль
    # без своей группы (-N): группа с тем же именем может уже быть (admin в Ubuntu) - кладём в группу службы
    id "$1" >/dev/null 2>&1 || { useradd -r -M -N -g "$RUN_GROUP" -d /nonexistent -s "$NOLOGIN" "$1"; echo "создан системный пользователь $1"; }
    printf '%s\n%s\n' "$2" "$2" | smbpasswd -s -a "$1" >/dev/null; smbpasswd -e "$1" >/dev/null
  }
  if [ ! -f /etc/samba/smb.conf ]; then   # Arch: пакет samba без smb.conf
    install -d /etc/samba
    printf '# создано start.sh (%s)\n[global]\n   workgroup = WORKGROUP\n   server string = %%h\n   map to guest = Bad User\n' "$APP" > /etc/samba/smb.conf
  fi
  smb_user "$SHARE_USER" "$SHARE_PASS"
  allow=$(echo "$SHARE_ALLOW" | tr ',' ' ')
  {
    echo "# $APP (создано start.sh). Подключается из smb.conf строкой include."
    echo "[$INSTALL_SMB_NAME]"
    echo "   path = $SHARE_ROOT"
    echo "   guest ok = yes"
    echo "   read only = yes"
    echo "   write list = $SHARE_USER"
    echo "   force user = $RUN_USER"
    echo "   force group = $RUN_GROUP"
    echo "   create mask = 0664"
    echo "   directory mask = 2775"
    echo "   hosts allow = $allow"
    veto=""; for h in ${SHARE_HIDE//,/ }; do veto+="/$h"; done
    [ -n "$veto" ] && echo "   veto files = $veto/"
  } > "$SMBINC"
  if [ -n "$SHARE_UPLOAD_USER" ]; then
    # vfs_worm в Debian 10-12 - в отдельном пакете samba-vfs-modules (в 13 и других дистрибутивах - уже в samba)
    worm_so || apt_optional samba-vfs-modules || true
    if worm_so; then
      smb_user "$SHARE_UPLOAD_USER" "$SHARE_UPLOAD_PASS"
      cat >> "$SMBINC" <<EOF

# только добавлять: vfs_worm запрещает менять/переименовывать/удалять то, чему больше $INSTALL_SMB_WORM_GRACE с
[$INSTALL_SMB_NAME-upload]
   path = $SHARE_ROOT/$SHARE_UPLOAD_DIR
   valid users = $SHARE_UPLOAD_USER
   read only = no
   force user = $RUN_USER
   force group = $RUN_GROUP
   create mask = 0664
   directory mask = 2775
   hosts allow = $allow
   vfs objects = worm
   worm:grace_period = $INSTALL_SMB_WORM_GRACE
EOF
      echo "шара для загрузки: $INSTALL_SMB_NAME-upload (= $SHARE_ROOT/$SHARE_UPLOAD_DIR)"
    else
      warn "в Samba нет модуля vfs_worm - шара только для загрузки не создана"
    fi
  fi
  grep -qxF "   include = $SMBINC" /etc/samba/smb.conf || printf '\n   include = %s\n' "$SMBINC" >> /etc/samba/smb.conf
  if [ "$(testparm -s --parameter-name='map to guest' 2>/dev/null)" = Never ]; then
    sed -i '0,/^\[global\]/s//[global]\n   map to guest = Bad User/' /etc/samba/smb.conf
  fi
  testparm -s >/dev/null 2>&1 || die "testparm нашёл ошибку в конфиге Samba"
  # SELinux (Fedora/RHEL): без этого smbd не пустят в общую папку
  if have selinuxenabled && selinuxenabled && have setsebool; then setsebool -P samba_export_all_rw on || true; fi
  SMBD=$(unit_of smbd smb)
  sctl enable "$SMBD" >/dev/null 2>&1; sctl restart "$SMBD" || true
  [ "$SYSTEMD" = 1 ] && open_port 445/tcp samba
  echo "Samba: $SMBINC (подключён из /etc/samba/smb.conf)"
elif [ -f "$SMBINC" ]; then
  rm -f "$SMBINC"; sed -i "\|^[[:space:]]*include = $SMBINC\$|d" /etc/samba/smb.conf
  sctl reload "$(unit_of smbd smb)" 2>/dev/null || true
fi

# ---------------------------------------------------------------- проверка
if [ "$SYSTEMD" = 0 ]; then
  echo "Запуск вручную: runuser -u $RUN_USER -- $INSTALL_DIR/venv/bin/python3 -I $INSTALL_DIR/share-web.py"
  exit 0
fi
say "Проверка"
ok=0
for _ in $(seq 1 20); do
  if "$INSTALL_DIR/venv/bin/python3" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3)" \
       "http://127.0.0.1:$SHARE_PORT/" 2>/dev/null; then ok=1; break; fi
  sleep 1
done
if [ "$ok" = 1 ]; then
  echo "${c_g}$SERVICE работает${c_0} (python из $INSTALL_DIR/venv)"
else
  systemctl --no-pager -l status "$SERVICE" | tail -15
  die "$SERVICE не отвечает на порту $SHARE_PORT"
fi
echo
echo "Адреса:"
for a in $(host_ips); do echo "  http://$a:$SHARE_PORT/"; done
echo "Полный доступ:   $SHARE_USER / $SHARE_PASS"
[ -n "$SHARE_UPLOAD_USER" ] && echo "Только загрузка: $SHARE_UPLOAD_USER / $SHARE_UPLOAD_PASS (в /$SHARE_UPLOAD_DIR)"
if [ "$INSTALL_LOG_DIR" != journal ]; then
  echo "Настройки: $cfg   Логи: $INSTALL_LOG_DIR/"
else
  echo "Настройки: $cfg   Логи: journalctl -u $SERVICE"
fi
