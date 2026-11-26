#!/usr/bin/env bash
# web-share: установка на Linux одной командой, без git clone и ручного скачивания.
#
#   curl -fsSL https://raw.githubusercontent.com/DrVidjet/web-share/HEAD/install.sh | sudo bash
#   curl -fsSL …/install.sh | sudo bash -s -- -y               # без вопросов
#   curl -fsSL …/install.sh | sudo bash -s -- --uninstall
#
# Скачивает проект (архив с GitHub) во временную папку и запускает из него start.sh с теми же
# параметрами; вопросы start.sh задаёт через терминал, даже когда скрипт пришёл через pipe.
# Повторный запуск = обновление. WEBSHARE_REPO / WEBSHARE_REF - другой репозиторий / ветка,
# WEBSHARE_SRC - взять проект из локальной папки (для CI).
set -euo pipefail

main() {
    local repo=${WEBSHARE_REPO:-DrVidjet/web-share} ref=${WEBSHARE_REF:-HEAD} src
    [ "$(id -u)" -eq 0 ] || { echo "Нужен root: curl -fsSL …/install.sh | sudo bash" >&2; exit 1; }
    WEBSHARE_TMP=$(mktemp -d)
    trap 'rm -rf "${WEBSHARE_TMP:-}"' EXIT
    if [ -n "${WEBSHARE_SRC:-}" ]; then
        src=$WEBSHARE_SRC
    else
        echo "Скачиваю $repo ($ref)…" >&2
        local url="https://github.com/$repo/archive/$ref.tar.gz"
        mkdir "$WEBSHARE_TMP/src"
        if command -v curl >/dev/null 2>&1; then curl -fsSL "$url"; else wget -qO- "$url"; fi |
            tar -xz -C "$WEBSHARE_TMP/src" --strip-components=1
        src=$WEBSHARE_TMP/src
    fi
    [ -f "$src/start.sh" ] || { echo "нет $src/start.sh" >&2; exit 1; }
    # stdin - это pipe со скриптом: вопросы читаем с терминала, если он есть (иначе start.sh берёт значения по умолчанию)
    if [ -t 0 ] || ! (exec </dev/tty) 2>/dev/null; then
        bash "$src/start.sh" "$@"
    else
        bash "$src/start.sh" "$@" </dev/tty
    fi
}

# всё в main: bash из pipe выполняет только целиком скачанный скрипт
main "$@"
