#!/usr/bin/env bash
# Uso: fix-album <nome do álbum> [artista]
#      fix-album -s <artista> <álbum> [mbid] [faixa...]   (singles soltos em Singles/<artista>)
# Normaliza as tags via beets, usando o MBID do .albuminfo (se existir).
set -euo pipefail

MUSIC=/srv/media/music

usage() {
  echo "uso: fix-album <nome do álbum> [artista]" >&2
  echo "     fix-album -s <artista> <álbum> [mbid] [faixa...]" >&2
  exit 1
}

# Lista as edições do álbum no MusicBrainz, uma por combinação de nº de faixas e formato.
# Saída: mbid <TAB> descrição
list_editions() {
  python3 - "$1" "$2" <<'PY'
import json, sys, urllib.parse, urllib.request
artist, album = sys.argv[1:3]
q = f'artist:"{artist}" AND release:"{album}" AND status:official'
url = "https://musicbrainz.org/ws/2/release?" + urllib.parse.urlencode(
    {"query": q, "fmt": "json", "limit": 100})
req = urllib.request.Request(url, headers={"User-Agent": "fix-album/1.0 (local)"})
releases = json.load(urllib.request.urlopen(req, timeout=30))["releases"]
FORMAT_RANK = {"Digital Media": 0, "CD": 1}
COUNTRY_RANK = {"XW": 0, "XE": 1, "US": 2, "GB": 3}
def rank(r):
    fmts = {m.get("format") or "?" for m in r.get("media", [])}
    return (min(FORMAT_RANK.get(f, 9) for f in fmts),
            COUNTRY_RANK.get(r.get("country"), 9),
            r.get("date") or "9999")
seen = {}
for r in sorted(releases, key=rank):
    if r["title"].lower() != album.lower():
        continue
    fmt = "+".join(sorted({m.get("format") or "?" for m in r.get("media", [])}))
    seen.setdefault((r.get("track-count"), fmt), r)
for (tracks, fmt), r in list(seen.items())[:8]:
    print(f'{r["id"]}\t{tracks} faixas, {fmt}, {r.get("date") or "?"}, {r.get("country") or "?"}')
PY
}

fix_singles() {
  [ $# -ge 2 ] || usage
  local artist=$1 album=$2 mbid=
  shift 2
  if [[ "${1:-}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    mbid=$1
    shift
  fi
  local tracks=("$@")

  mapfile -t dirs < <(find "$MUSIC/Singles" -mindepth 1 -maxdepth 1 -type d -iname "*${artist}*" | sort)
  if [ ${#dirs[@]} -ne 1 ]; then
    echo "esperava uma pasta de singles para '$artist', achei ${#dirs[@]}:" >&2
    printf '  %s\n' "${dirs[@]#$MUSIC/}" >&2
    exit 1
  fi
  local dir=${dirs[0]}
  echo "singles: ${dir#$MUSIC/}"

  if [ -z "$mbid" ]; then
    mapfile -t editions < <(list_editions "$artist" "$album")
    if [ ${#editions[@]} -eq 0 ]; then
      echo "nenhuma edição de '$album' de '$artist' no MusicBrainz" >&2
      exit 1
    fi
    echo "edições de '$album':"
    local i=1
    for e in "${editions[@]}"; do
      printf '  %d) %s   %s\n' "$i" "${e#*$'\t'}" "${e%%$'\t'*}"
      i=$((i + 1))
    done
    if [ ! -t 0 ]; then
      echo "escolha uma e rode de novo com o mbid: fix-album -s \"$artist\" \"$album\" <mbid>" >&2
      exit 1
    fi
    read -r -p "qual edição? " choice
    [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#editions[@]} ] || { echo "inválido" >&2; exit 1; }
    mbid=${editions[$((choice - 1))]%%$'\t'*}
  fi

  echo "release MusicBrainz: $mbid"

  local config=()
  if [ ${#tracks[@]} -gt 0 ]; then
    local overlay
    overlay=$(mktemp --suffix=.yaml)
    trap 'rm -f "$overlay"' EXIT
    # O beets importa a pasta toda; os arquivos fora da lista entram como "ignorados".
    python3 - "$dir" "$overlay" "${tracks[@]}" <<'PY'
import glob, json, os, sys
folder, overlay, *wanted = sys.argv[1:]
exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"}
files = sorted(f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in exts)
keep = set()
for name in wanted:
    hits = [f for f in files if name.lower() in os.path.splitext(f)[0].lower()]
    if len(hits) != 1:
        print(f"'{name}': " + ("nenhum arquivo" if not hits else "mais de um arquivo: " + ", ".join(hits)), file=sys.stderr)
        sys.exit(1)
    keep.add(hits[0])
print("faixas: " + ", ".join(sorted(keep)))
ignore = [".*", "*~", "System Volume Information", "lost+found"]
ignore += [glob.escape(f) for f in files if f not in keep]
json.dump({"ignore": ignore}, open(overlay, "w"))
PY
    config=(-c "$overlay")
  fi

  beet "${config[@]}" import --search-id "$mbid" "$dir"
}

[ $# -ge 1 ] || usage

if [ "$1" = "-s" ]; then
  shift
  fix_singles "$@"
  exit
fi

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
