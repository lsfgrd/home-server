#!/usr/bin/env python3
"""Organiza a biblioteca de música no formato que o MusicGrabber usa.

    Albums/Artista/Álbum/NN - Título.ext         (multi-disco: DD-NN - Título.ext)
    Singles/Artista/Título.ext

Lê as tags de cada arquivo fora de Albums/ (pastas soltas, _playlists,
Metallica/, Clara Nunes/...) e decide se a faixa é single ou parte de um álbum:

  - já existe Albums/Artista/Álbum         -> vai para lá
  - o grupo tem >= --min-album-tracks      -> cria o álbum
  - senão                                  -> Singles/Artista/
  - arquivos em Singles/ cujo álbum existe -> sobem para o álbum
  - faixa fora de Albums/ com mesmo artista e título de uma já em Albums/
    (mesmo que o nome do álbum difira, ex.: "Vol. 1" x "Volume One")
                                           -> é removida (a de Albums/ fica)

Arquivos em Albums/ nunca são tocados. Os .lrc seguem a faixa e as capas
(cover/folder/front) seguem se a pasta de origem inteira vai para um só destino.
Nada é sobrescrito; os únicos arquivos apagados são essas duplicatas (e o .lrc
delas). Pastas que ficarem vazias também são removidas.

Por padrão só mostra o plano. Use --apply para mover de verdade.
Precisa de ffprobe; sem ele no host, usa o do container do Navidrome.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

AUDIO = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".alac", ".wma"}
CAPAS = {"cover", "folder", "front"}
IMAGENS = {".jpg", ".jpeg", ".png"}


def probe(arq, args):
    if shutil.which("ffprobe"):
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format_tags",
               "-of", "json", str(arq)]
    else:
        dentro = Path(args.container_music) / arq.relative_to(args.root)
        cmd = ["docker", "exec", args.container, "ffprobe", "-v", "error",
               "-show_entries", "format_tags", "-of", "json", str(dentro)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    tags = json.loads(r.stdout or "{}").get("format", {}).get("tags", {})
    return {k.lower(): v.strip() for k, v in tags.items()}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


EDICAO = re.compile(r"\s*[\(\[]([^\)\]]*(remaster|deluxe|edition|re-?issue|expanded|anniversary|version)"
                    r"[^\)\]]*|\d{4})[\)\]]\s*$", re.I)


def norm_album(s):
    """Ignora sufixos de edição, como (Remastered), (Deluxe Edition) ou (2004)."""
    return norm(EDICAO.sub("", s or ""))


PREFIXO_FAIXA = re.compile(r"^(\d+-)?\d+\s*-\s+")


def titulo_do_arquivo(arq):
    """Título de um arquivo de Albums/ pelo nome (tira o prefixo "NN - " ou "DD-NN - ")."""
    return PREFIXO_FAIXA.sub("", arq.stem)


def limpa(s):
    s = re.sub(r"[\x00-\x1f]", "", s).replace("/", "_").replace("\\", "_")
    return s.strip(" .") or "_"


def inteiro(v):
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else None


def total(v):
    m = re.match(r"\s*\d+\s*/\s*(\d+)", v or "")
    return int(m.group(1)) if m else None


class Faixa:
    def __init__(self, arq, tags):
        self.arq = arq
        self.titulo = tags.get("title") or arq.stem
        self.artista = tags.get("album_artist") or tags.get("albumartist") \
            or tags.get("artist") or "Desconhecido"
        self.album = tags.get("album", "")
        self.n = inteiro(tags.get("track"))
        self.disco = inteiro(tags.get("disc")) or 1
        self.discos = (inteiro(tags.get("disctotal")) or inteiro(tags.get("totaldiscs"))
                       or total(tags.get("disc")) or 1)
        self.ano = (tags.get("date") or tags.get("year") or "")[:4]

    @property
    def chave_album(self):
        return (norm(self.artista), norm_album(self.album))

    def nome_album(self, ano):
        ext = self.arq.suffix.lower()
        if self.n is None:
            return f"{limpa(self.titulo)}{ext}"
        pref = f"{self.disco:02d}-{self.n:02d}" if self.discos > 1 else f"{self.n:02d}"
        return f"{pref} - {limpa(self.titulo)}{ext}"

    def nome_single(self):
        return f"{limpa(self.titulo)}{self.arq.suffix.lower()}"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", type=Path, default=Path("/srv/media/music"))
    p.add_argument("--apply", action="store_true", help="move de verdade (padrão: só mostra)")
    p.add_argument("--min-album-tracks", type=int, default=3,
                   help="faixas do mesmo álbum para criar a pasta de álbum (padrão: 3)")
    p.add_argument("--year", action="store_true", help='pasta do álbum como "Álbum (2004)"')
    p.add_argument("--container", default="navidrome", help="container com ffprobe")
    p.add_argument("--container-music", default="/music", help="onde a biblioteca fica no container")
    args = p.parse_args()

    root = args.root.resolve()
    args.root = root
    albums_dir, singles_dir = root / "Albums", root / "Singles"

    # álbuns que já existem: (artista, álbum) -> pasta
    existentes = {}
    for pasta in albums_dir.glob("*/*"):
        if pasta.is_dir():
            existentes[(norm(pasta.parent.name), norm_album(pasta.name))] = pasta

    # faixas que já estão em Albums/: (artista, título) pelo caminho/nome do arquivo
    em_albums = {(norm(a.parent.parent.name), norm(titulo_do_arquivo(a)))
                 for a in albums_dir.glob("*/*/*")
                 if a.suffix.lower() in AUDIO and a.is_file()}

    # arquivos a avaliar: tudo fora de Albums/ (em Singles/ só para subir a álbum)
    faixas = []
    for arq in sorted(root.rglob("*")):
        if (arq.suffix.lower() in AUDIO and arq.is_file() and albums_dir not in arq.parents):
            faixas.append(Faixa(arq, probe(arq, args)))
    if not faixas:
        print("Nada fora de Albums/ para organizar.")
        return

    destinos = {}   # Faixa -> (tipo, destino)
    grupos = defaultdict(list)
    for f in faixas:
        if (norm(f.artista), norm(f.titulo)) in em_albums:
            destinos[f] = ("remover", None)
        else:
            grupos[f.chave_album].append(f)

    def nome_canonico(grupo):
        """Nome de álbum mais comum entre as faixas (empate: o mais curto)."""
        nomes = [f.album for f in grupo]
        return max(set(nomes), key=lambda n: (nomes.count(n), -len(n)))

    def pasta_album(f, album):
        if f.chave_album in existentes:
            return existentes[f.chave_album]
        nome = limpa(album) + (f" ({f.ano})" if args.year and f.ano else "")
        return albums_dir / limpa(f.artista) / nome

    for chave, grupo in grupos.items():
        sem_album = not chave[1]
        so_single = len(grupo) == 1 and norm(grupo[0].album) == norm(grupo[0].titulo)
        eh_album = (not sem_album and not so_single
                    and (chave in existentes or len(grupo) >= args.min_album_tracks))
        canonico = nome_canonico(grupo)
        for f in grupo:
            em_singles = singles_dir in f.arq.parents
            if eh_album:
                destinos[f] = ("album", pasta_album(f, canonico) / f.nome_album(args.year))
            elif em_singles:
                continue   # single que fica onde está
            else:
                destinos[f] = ("single", singles_dir / limpa(f.artista) / f.nome_single())

    # sidecars (.lrc) acompanham a faixa; capas acompanham se a pasta vai toda para um lugar
    movimentos = []   # (tipo, origem, destino)
    usados = set()
    conflitos = []
    for f, (tipo, dst) in destinos.items():
        if f.arq == dst:
            continue
        pares = [(f.arq, dst)]
        lrc = f.arq.with_suffix(".lrc")
        if lrc.exists():
            pares.append((lrc, dst and dst.with_suffix(".lrc")))
        if tipo == "remover":
            movimentos.extend((tipo, o, None) for o, _ in pares)
            continue
        if any(d in usados or (d.exists() and d != o) for o, d in pares):
            conflitos.append((f.arq, dst))
            continue
        for o, d in pares:
            usados.add(d)
            movimentos.append((tipo if o == f.arq else "letra", o, d))

    por_origem = defaultdict(set)
    for f in faixas:
        if f in destinos and destinos[f][0] != "remover":
            por_origem[f.arq.parent].add(destinos[f][1].parent)
    for pasta, dsts in por_origem.items():
        if len(dsts) != 1 or pasta == root:
            continue
        # só se todo o áudio da pasta está sendo movido
        if any(a.suffix.lower() in AUDIO and a not in {m[1] for m in movimentos}
               for a in pasta.iterdir()):
            continue
        dst_dir = next(iter(dsts))
        for img in pasta.iterdir():
            if img.suffix.lower() in IMAGENS and img.stem.lower() in CAPAS:
                d = dst_dir / ("cover" + img.suffix.lower())
                if d.exists() or d in usados:
                    continue
                usados.add(d)
                movimentos.append(("capa", img, d))

    # saída
    rel = lambda x: x.relative_to(root)
    for tipo, o, d in movimentos:
        if d is None:
            print(f"[{tipo:6}] {rel(o)}")
        else:
            print(f"[{tipo:6}] {rel(o)}\n         -> {rel(d)}")
    for o, d in conflitos:
        print(f"[CONFLITO] {rel(o)} -> {rel(d)} (destino já existe; ignorado)")
    cont = defaultdict(int)
    for tipo, *_ in movimentos:
        cont[tipo] += 1
    print("\nResumo: " + ", ".join(f"{n} {t}" for t, n in sorted(cont.items()))
          + f", {len(conflitos)} conflitos")

    if not args.apply:
        print("Nada foi movido. Rode com --apply para executar.")
        return

    for _, o, d in movimentos:
        if d is None:
            o.unlink()
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(o), str(d))
    # remove pastas que ficaram vazias (nunca a raiz, Albums ou Singles)
    for pasta in sorted((x for x in root.rglob("*") if x.is_dir()),
                        key=lambda x: len(x.parts), reverse=True):
        if pasta not in (albums_dir, singles_dir) and not any(pasta.iterdir()):
            pasta.rmdir()
    print("Pronto. O Navidrome pega as mudanças no próximo scan.")


if __name__ == "__main__":
    sys.exit(main())
