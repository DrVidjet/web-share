#!/bin/bash
# web-share: установка / обновление / удаление на Debian 10-13.
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
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
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

check_debian() {
  . /etc/os-release
  [ "${ID:-}" = debian ] || [[ " ${ID_LIKE:-} " == *" debian "* ]] || die "Нужен Debian (тут: ${PRETTY_NAME:-?})"
  local dv; dv=$(cut -d. -f1 /etc/debian_version 2>/dev/null)   # у производных (Astra и т.п.) - версия базы Debian
  case $dv in 10|11|12|13) [ "${ID:-}" = debian ] || echo "${PRETTY_NAME:-?} (база Debian $dv)" ;;
    *) warn "Проверено на Debian 10-13, тут ${PRETTY_NAME:-?} ($(cat /etc/debian_version 2>/dev/null)) - пробую" ;; esac
}
APT_UPDATED=0
apt_update() {
  [ "$APT_UPDATED" = 1 ] && return 0
  if ! apt-get update -q; then
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
  APT_UPDATED=1
}
need_pkgs() {
  local miss=() p
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || miss+=("$p")
  done
  [ ${#miss[@]} = 0 ] && return 0
  say "Ставлю пакеты: ${miss[*]}"
  apt_update
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${miss[@]}"
}
make_venv() {   # make_venv папка
  local v=$1/venv
  if [ -x "$v/bin/python3" ] && "$v/bin/python3" -c 'import sys' 2>/dev/null; then return 0; fi
  rm -rf "$v"
  python3 -m venv "$v" || die "не удалось создать venv в $v"
  echo "создано виртуальное окружение $v ($("$v/bin/python3" -V 2>&1))"
}
host_ips() { ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1; }
set_shebang() { sed -i "1s|.*|#!$2 -I|" "$1"; }  # set_shebang файл python

# ---------------------------------------------------------------- ответы
check_debian
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

if [ "$UNINSTALL" = 1 ]; then
  say "Удаление $SERVICE"
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT" "$CRON"
  if [ -f "$SMBINC" ]; then
    rm -f "$SMBINC"; sed -i "\|^[[:space:]]*include = $SMBINC\$|d" /etc/samba/smb.conf
    systemctl reload smbd 2>/dev/null || true
  fi
  systemctl daemon-reload
  ask_yn DEL_DIR "Удалить папку установки $INSTALL_DIR (код, venv, .env)?" y
  [ "$DEL_DIR" = y ] && rm -rf "$INSTALL_DIR"
  echo "Готово. Общая папка ${PRE[SHARE_ROOT]:-} и пользователи не тронуты."
  exit 0
fi

ask SHARE_ROOT "Общая папка (корень)" "/srv/share"
SHARE_ROOT=${SHARE_ROOT%/}
ask SHARE_PORT "Порт веб-интерфейса" "8081"
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
ask INSTALL_DOCKER_INDEX_DIR "Папка с docker-образами для индекса images.list (пусто — не нужно)" ""
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
setting INSTALL_SMB_WORM_GRACE 30
RUN_USER=$INSTALL_RUN_USER
# ---------------------------------------------------------------- установка
need_pkgs python3 python3-venv ca-certificates
[ -n "$INSTALL_DOCKER_INDEX_DIR" ] && need_pkgs cron
[ "$INSTALL_SAMBA" = y ] && need_pkgs samba

say "Пользователь $RUN_USER и общая папка $SHARE_ROOT"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  useradd -r -M -U -d /nonexistent -s /usr/sbin/nologin "$RUN_USER"
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
for d in ${SHARE_UPLOAD_DIR:+"$SHARE_ROOT/$SHARE_UPLOAD_DIR"} ${INSTALL_DOCKER_INDEX_DIR:+"$INSTALL_DOCKER_INDEX_DIR"}; do
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
           SHARE_UPLOAD_USER SHARE_UPLOAD_PASS SHARE_UPLOAD_DIR SHARE_SESSION_HOURS; do echo "$k=${!k}"; done
  echo "# для start.sh (приложение не читает)"
  for k in INSTALL_SERVICE INSTALL_RUN_USER INSTALL_DOCKER_INDEX_DIR INSTALL_SAMBA INSTALL_SMB_NAME INSTALL_SMB_WORM_GRACE; do echo "$k=${!k-}"; done
} > "$cfg.tmp"
umask 022
chown "root:$RUN_GROUP" "$cfg.tmp"; chmod 640 "$cfg.tmp"; mv "$cfg.tmp" "$cfg"
rm -f "$INSTALL_DIR/config.env"
echo "настройки: $cfg"

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
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

if [ -n "$INSTALL_DOCKER_INDEX_DIR" ]; then
  echo "*/5 * * * * $RUN_USER $INSTALL_DIR/share-images-index.sh $INSTALL_DOCKER_INDEX_DIR" > "$CRON"
  chmod 644 "$CRON"
  systemctl enable --now cron >/dev/null 2>&1 || true
  echo "cron: $CRON (индекс $INSTALL_DOCKER_INDEX_DIR/images.list раз в 5 минут)"
else
  rm -f "$CRON"
fi

# ---------------------------------------------------------------- Samba
if [ "$INSTALL_SAMBA" = y ]; then
  say "Samba"
  smb_user() {  # smb_user логин пароль
    id "$1" >/dev/null 2>&1 || { useradd -r -M -d /nonexistent -s /usr/sbin/nologin "$1"; echo "создан системный пользователь $1"; }
    printf '%s\n%s\n' "$2" "$2" | smbpasswd -s -a "$1" >/dev/null; smbpasswd -e "$1" >/dev/null
  }
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
  } > "$SMBINC"
  if [ -n "$SHARE_UPLOAD_USER" ]; then
    # vfs_worm в Debian 10-12 - в отдельном пакете samba-vfs-modules (в 13 - уже в samba)
    if ! find /usr/lib -path '*samba/vfs/worm.so' 2>/dev/null | grep -q . && \
       apt-cache show samba-vfs-modules >/dev/null 2>&1; then
      need_pkgs samba-vfs-modules
    fi
    if find /usr/lib -path '*samba/vfs/worm.so' 2>/dev/null | grep -q .; then
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
  systemctl enable smbd >/dev/null 2>&1; systemctl restart smbd
  echo "Samba: $SMBINC (подключён из /etc/samba/smb.conf)"
elif [ -f "$SMBINC" ]; then
  rm -f "$SMBINC"; sed -i "\|^[[:space:]]*include = $SMBINC\$|d" /etc/samba/smb.conf
  systemctl reload smbd 2>/dev/null || true
fi

# ---------------------------------------------------------------- проверка
say "Проверка"
ok=0
for i in $(seq 1 20); do
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
echo "Настройки: $cfg   Логи: journalctl -u $SERVICE"
