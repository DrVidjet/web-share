#!/bin/bash
# Индекс docker-образов: <папка>/images.list. Использование: share-images-index.sh <папка с docker-образами>
# Строки: <образ:тег> <путь от docker-images> <размер> <mtime>. Файлы ищутся во всех подпапках.
# Файлы - результат "docker save" (.tar) или он же сжатый (.gz/.tgz/.tar.gz).
# Теги читаются из manifest.json внутри архива; для неизменившихся файлов берутся из прошлого индекса.
# Запускается cron'ом (файл в /etc/cron.d создаёт start.sh) раз в 5 минут.
D="${1:?укажите папку с docker-образами}"
L="$D/images.list"
exec 9> /tmp/share-images-index.lock
flock -n 9 || exit 0
cd "$D" || exit 1

tmp=$(mktemp)
now=$(date +%s)
find "$D" -mindepth 1 -type f ! -path '*/.*' \( -name '*.tar' -o -name '*.tgz' -o -name '*.gz' \) -printf '%P\n' | sort |
while IFS= read -r b; do
  case "$b" in *[[:space:]]*) echo "Пропуск (пробел в пути): $b" >&2; continue ;; esac
  f="$D/$b"
  set -- $(stat -c '%s %Y' "$f"); size=$1; mtime=$2
  # файл ещё копируется
  [ $((now - mtime)) -lt 120 ] && continue
  if [ -f "$L" ] && awk -v f="$b" -v s="$size" -v m="$mtime" '$2==f && $3==s && $4==m {found=1} END {exit !found}' "$L"; then
    awk -v f="$b" -v s="$size" -v m="$mtime" '$2==f && $3==s && $4==m' "$L" >> "$tmp"
    continue
  fi
  case "$b" in
    *.tar) man=$(tar -xOf "$f" --occurrence=1 manifest.json 2>/dev/null) ;;
    *)     man=$(gzip -cd "$f" 2>/dev/null | tar -xOf - --occurrence=1 manifest.json 2>/dev/null) ;;
  esac
  tags=$(printf '%s' "$man" | grep -o '"RepoTags":\[[^]]*\]' | sed -e 's/^"RepoTags":\[//' -e 's/\]$//' | tr ',' '\n' | tr -d '" ')
  for t in $tags; do
    echo "$t $b $size $mtime" >> "$tmp"
  done
  [ -n "$tags" ] || echo "Нет тегов в $b" >&2
done
sort -u "$tmp" > "$tmp.s"
if ! cmp -s "$tmp.s" "$L"; then
  cat "$tmp.s" > "$L.new" && mv -f "$L.new" "$L"
  chmod 664 "$L"
fi
rm -f "$tmp" "$tmp.s"
