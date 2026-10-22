#!/usr/bin/env bash
# Uso: ./compartilhar.sh <pasta dentro de /srv/media/music> [...]
# Ex.: ./compartilhar.sh "Albums/Metallica/And Justice For All"
# Cria hardlinks (sem gastar espaço) em /srv/media/shares com o mesmo caminho,
# e o slskd passa a compartilhar. Para parar de compartilhar, apague de lá.
set -euo pipefail
lib=/srv/media/music
shares=/srv/media/shares
[ $# -gt 0 ] || { echo "Uso: $0 <pasta em $lib> [...]"; exit 1; }
for rel in "$@"; do
  rel="${rel#"$lib"/}"
  [ -e "$lib/$rel" ] || { echo "Não existe: $lib/$rel"; exit 1; }
  mkdir -p "$shares/$(dirname "$rel")"
  cp -al "$lib/$rel" "$shares/$rel"
  echo "Compartilhado: $rel"
done
