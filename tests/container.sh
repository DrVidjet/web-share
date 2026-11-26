#!/usr/bin/env bash
# Проверка start.sh в контейнере без systemd (CI гоняет её на Debian, Ubuntu, Fedora, Arch, openSUSE):
# ставит пакеты, venv, пользователя, .env, unit, cron, logrotate, Samba; затем запускает программу
# вручную от пользователя службы и проверяет HTTP, индекс docker-образов и --uninstall.
#   docker run --rm -v "$PWD:/src:ro" debian:12 bash /src/tests/container.sh
set -uo pipefail

SRC=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
pass=0 fail=0
ok() { echo "  ok    $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "        $2"; fail=$((fail + 1)); }
check() { local n=$1; shift; if "$@" >/dev/null 2>&1; then ok "$n"; else bad "$n" "$*"; fi; }

A=$(mktemp)
cat >"$A" <<'EOF'
INSTALL_DIR=/opt/web-share
SHARE_ROOT=/srv/share-test
SHARE_PORT=18081
SHARE_PASS=adminpw
SHARE_UPLOAD_PASS=uppw
INSTALL_DOCKER_INDEX_DIR=docker-images
INSTALL_SAMBA=y
INSTALL_SMB_NAME=share
SHARE_SMB_PATHS=\\host\share
EOF

echo "== start.sh -y"
if bash "$SRC/start.sh" -y --answers "$A"; then ok "start.sh отработал"; else bad "start.sh отработал"; fi
echo "== повторный запуск (обновление)"
if bash "$SRC/start.sh" -y; then ok "повторный start.sh"; else bad "повторный start.sh"; fi

D=/opt/web-share
check "venv" "$D/venv/bin/python3" -c 'import sys; assert sys.version_info >= (3, 7)'
check "share-web.py с python из venv" grep -q "^#!$D/venv/bin/python3 -I" "$D/share-web.py"
check ".env: настройки" grep -qx 'SHARE_PORT=18081' "$D/.env"
check ".env: пароль сохранён при обновлении" grep -qx 'SHARE_PASS=adminpw' "$D/.env"
check ".env: права 640" test "$(stat -c %a "$D/.env")" = 640
check "пользователь webshare" id webshare
check "папка загрузки" test -d /srv/share-test/upload
check "папка docker-образов (от SHARE_ROOT)" test -d /srv/share-test/docker-images
check "unit systemd" grep -q "ExecStart=$D/venv/bin/python3 -I $D/share-web.py" /etc/systemd/system/web-share.service
check "cron индекса" grep -q "share-images-index.sh /srv/share-test/docker-images" /etc/cron.d/web-share-images-index
check "logrotate" test -f /etc/logrotate.d/web-share
check "logrotate: конфиг верный" logrotate -d /etc/logrotate.d/web-share
check "samba: include" grep -q "include = /etc/samba/web-share.conf" /etc/samba/smb.conf
check "samba: testparm" testparm -s
check "samba: lost+found скрыт" grep -q "veto files = /lost+found/" /etc/samba/web-share.conf

echo "== запуск программы от webshare"
runuser -u webshare -- "$D/venv/bin/python3" -I "$D/share-web.py" >/tmp/web.log 2>&1 &
PID=$!
up=0
for _ in $(seq 50); do
    "$D/venv/bin/python3" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:18081/api/version", timeout=2)' 2>/dev/null && { up=1; break; }
    sleep 0.2
done
if [ "$up" = 1 ]; then ok "отвечает на 18081"; else bad "отвечает на 18081" "$(tail -5 /tmp/web.log)"; fi
check "загрузка от admin" "$D/venv/bin/python3" - <<'EOF'
import json, urllib.request
o = urllib.request.build_opener()
r = o.open(urllib.request.Request("http://127.0.0.1:18081/api/login", json.dumps({"user": "admin", "pass": "adminpw"}).encode(),
                                  {"Content-Type": "application/json"}))
cookie = r.headers["Set-Cookie"].split(";")[0]
o.open(urllib.request.Request("http://127.0.0.1:18081/api/upload?path=&name=hello.txt", b"hi", {"Cookie": cookie}, method="PUT"))
EOF
check "файл создан от webshare" test "$(stat -c %U /srv/share-test/hello.txt 2>/dev/null)" = webshare
kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null

echo "== индекс docker-образов"
T=$(mktemp -d)
printf '[{"Config":"x.json","RepoTags":["demo/app:1.0"],"Layers":[]}]' >"$T/manifest.json"
tar -C "$T" -cf /srv/share-test/docker-images/app.tar manifest.json
touch -d '-10 minutes' /srv/share-test/docker-images/app.tar
chown -R webshare: /srv/share-test/docker-images
runuser -u webshare -- "$D/share-images-index.sh" /srv/share-test/docker-images
check "images.list" grep -q '^demo/app:1.0 app.tar ' /srv/share-test/docker-images/images.list

echo "== --uninstall"
check "start.sh --uninstall" bash "$SRC/start.sh" -y --uninstall
check "unit удалён" test ! -e /etc/systemd/system/web-share.service
check "папка установки удалена" test ! -e "$D"
check "samba include убран" bash -c '! grep -q web-share.conf /etc/samba/smb.conf'
check "общая папка на месте" test -f /srv/share-test/hello.txt

echo
echo "итого: ok $pass, FAIL $fail"
[ "$fail" = 0 ]
