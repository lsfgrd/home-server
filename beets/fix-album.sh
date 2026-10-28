#!/usr/bin/env bash
# Uso: fix-album <nome do álbum> [artista]
# Normaliza as tags via beets, usando o MBID do .albuminfo (se existir).
set -euo pipefail

MUSIC=/srv/media/music

usage() {
  echo "uso: fix-album <nome do álbum> [artista]" >&2
  exit 1
}

[ $# -ge 1 ] || usage

ALBUMS=$MUSIC/Albums
album=$1
artist=${2:-}

mapfile -t dirs < <(
  find "$ALBUMS" -mindepth 2 -maxdepth 2 -type d -ipath "*${artist}*/*${album}*" | sort
)

if [ ${#dirs[@]} -eq 0 ]; then
  echo "nenhum álbum encontrado para '$album'${artist:+ de '$artist'}" >&2
  exit 1
fi

if [ ${#dirs[@]} -gt 1 ]; then
  echo "mais de um álbum encontrado, passe o artista como 2º argumento:" >&2
  printf '  %s\n' "${dirs[@]#$ALBUMS/}" >&2
  exit 1
fi

dir=${dirs[0]}
echo "álbum: ${dir#$ALBUMS/}"

mbid=
if [ -f "$dir/.albuminfo" ]; then
  mbid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("release_mbid",""))' "$dir/.albuminfo")
fi

if [ -n "$mbid" ]; then
  echo "release MusicBrainz: $mbid"
  beet import --search-id "$mbid" "$dir"
else
  echo "sem .albuminfo, buscando no MusicBrainz pelo nome"
  beet import "$dir"
fi
