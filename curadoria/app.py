#!/usr/bin/env python3
"""Curadoria: revisa álbuns baixados do Soulseek e move para a biblioteca do Navidrome.

Destino:
    Albums/Artista/Álbum/NN - Título.ext       (multi-disco: DD-NN - Título.ext)

Compartilhar (Soulseek): cria hardlinks do álbum em SHARES com o mesmo caminho relativo
(sem gastar espaço); o slskd só enxerga essa pasta. Parar de compartilhar apaga os links.

Nada é sobrescrito: faixas que já existem no destino (mesmo título) são puladas e
ficam na pasta de origem para você decidir.
"""
import difflib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.id3 import COMM
from flask import Flask, jsonify, request, send_from_directory, abort

DOWNLOADS = Path(os.environ.get("DOWNLOADS_DIR", "/media/downloads/soulseek")).resolve()
MUSIC = Path(os.environ.get("MUSIC_DIR", "/media/music")).resolve()
ALBUMS = MUSIC / "Albums"
SHARES = Path(os.environ.get("SHARES_DIR", "/media/shares")).resolve()
UA = "curadoria/1.0 (local)"

AUDIO = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".alac", ".wma"}
CAPAS = {"cover", "folder", "front", "albumart", "album"}
IMAGENS = {".jpg", ".jpeg", ".png"}
LIXO = {".nfo", ".m3u", ".m3u8", ".log", ".cue", ".sfv", ".txt", ".url", ".db", ".ini", ".ds_store", ".accurip"}

EasyMP4Tags.RegisterFreeformKey("musicbrainz_albumid", "MusicBrainz Album Id")
EasyMP4Tags.RegisterTextKey("comment", "\xa9cmt")
EasyID3.RegisterKey("comment", getter=lambda id3, k: [t for c in id3.getall("COMM") for t in c.text],
                    setter=lambda id3, k, v: (id3.delall("COMM"), id3.add(COMM(encoding=3, lang="eng", desc="", text=v)))[-1],
                    deleter=lambda id3, k: id3.delall("COMM"))

# tags que a tela edita com campos próprios; o resto vai para "Outras tags"
GERENCIADAS = {"title", "artist", "albumartist", "album", "date", "tracknumber", "discnumber",
               "musicbrainz_albumid"}
CHAVE_OK = re.compile(r"^[a-z0-9_]{1,40}$")

app = Flask(__name__, static_folder="static")


# ---------- utilidades ----------

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


EDICAO = re.compile(r"\s*[\(\[]([^\)\]]*(remaster|deluxe|edition|re-?issue|expanded|anniversary|version)"
                    r"[^\)\]]*|\d{4})[\)\]]\s*$", re.I)


def norm_album(s):
    return norm(EDICAO.sub("", s or ""))


def limpa(s):
    s = re.sub(r"[\x00-\x1f]", "", s or "").replace("/", "_").replace("\\", "_")
    return s.strip(" .") or "_"


def inteiro(v):
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def first(tags, key):
    v = tags.get(key) if tags else None
    return (v[0] if isinstance(v, list) and v else v) or ""


def seguro(rel):
    """Pasta de origem pelo nome relativo, sempre dentro de DOWNLOADS."""
    p = (DOWNLOADS / rel).resolve()
    if p == DOWNLOADS or DOWNLOADS not in p.parents:
        abort(400, "caminho inválido")
    return p


def audio_files(pasta):
    return sorted(a for a in pasta.rglob("*") if a.is_file() and a.suffix.lower() in AUDIO)


PREFIXO = re.compile(r"^\s*(?:(\d+)\s*[-.]\s*)?(\d{1,3})\s*[-.\s]+\s*")
DISCO_DIR = re.compile(r"(?:cd|disc|disco|disk)\s*(\d+)", re.I)


def le_tags(arq):
    try:
        m = mutagen.File(arq, easy=True)
    except Exception:
        m = None
    tags = m.tags if m is not None and m.tags is not None else {}
    info = getattr(m, "info", None)
    return tags, {
        "formato": arq.suffix.lower().lstrip("."),
        "bitrate": int(getattr(info, "bitrate", 0) or 0) // 1000,
        "duracao": int(getattr(info, "length", 0) or 0),
    }


def faixa_de(arq, pasta):
    tags, info = le_tags(arq)
    stem = arq.stem
    m = PREFIXO.match(stem)
    disc_arq, n_arq = (inteiro(m.group(1)), inteiro(m.group(2))) if m else (None, None)
    titulo_arq = PREFIXO.sub("", stem)
    dm = DISCO_DIR.search(arq.parent.name) if arq.parent != pasta else None
    track = first(tags, "tracknumber")
    disc = first(tags, "discnumber")
    return {
        "arquivo": str(arq.relative_to(pasta)),
        "titulo": first(tags, "title") or titulo_arq,
        "artista": first(tags, "artist"),
        "artista_album": first(tags, "albumartist"),
        "album": first(tags, "album"),
        "ano": first(tags, "date")[:4],
        "faixa": inteiro(track) or n_arq,
        "disco": inteiro(disc) or disc_arq or (int(dm.group(1)) if dm else 1),
        "mbid": first(tags, "musicbrainz_albumid"),
        "extras": {k.lower(): [str(x) for x in (v if isinstance(v, list) else [v])]
                   for k, v in dict(tags).items() if k.lower() not in GERENCIADAS},
        "tem_tags": bool(first(tags, "title") and first(tags, "artist")),
        **info,
    }


def mais_comum(valores, padrao=""):
    vs = [v for v in valores if v]
    return max(set(vs), key=lambda v: (vs.count(v), -len(v))) if vs else padrao


def acha_capa(pasta):
    cands = [a for a in pasta.rglob("*")
             if a.is_file() and a.suffix.lower() in IMAGENS
             and re.sub(r"\s*\(\d+\)$", "", a.stem.lower()) in CAPAS]
    return min(cands, key=lambda a: (len(a.parts), a.name), default=None)


def acha_existente(pai, nome, f=norm):
    """Pasta já existente em `pai` equivalente a `nome` (ignora caixa, acentos, pontuação)."""
    if not pai.is_dir():
        return None
    alvo = f(nome)
    for d in pai.iterdir():
        if d.is_dir() and f(d.name) == alvo:
            return d
    return None


def titulo_de_arquivo_lib(arq):
    return norm(re.sub(r"^(\d+-)?\d+\s*-\s+", "", arq.stem))


def rascunho(pasta):
    arqs = audio_files(pasta)
    faixas = [faixa_de(a, pasta) for a in arqs]
    artista = mais_comum([f["artista_album"] for f in faixas]) or mais_comum([f["artista"] for f in faixas])
    if not artista:   # "Artista - Título.ext"
        cands = [PREFIXO.sub("", a.stem).split(" - ", 1)[0] for a in arqs if " - " in PREFIXO.sub("", a.stem)]
        artista = mais_comum(cands)
    album = mais_comum([f["album"] for f in faixas])
    if not album:
        album = re.sub(r"^\s*\d{4}\s*-\s*|\s*[\(\[]\d{4}[\)\]]\s*$", "", pasta.name).strip()
    ano = mais_comum([f["ano"] for f in faixas])
    if not ano:
        m = re.search(r"(?:^|[\(\[\s])((?:19|20)\d{2})(?:[\)\]\s-]|$)", pasta.name)
        ano = m.group(1) if m else ""
    for f in faixas:
        # artista de faixa só quando difere do artista do álbum
        if f["artista"] == artista:
            f["artista"] = ""
        if not f["artista"] and artista:
            p = PREFIXO.sub("", Path(f["arquivo"]).stem)
            if p.lower().startswith(artista.lower() + " - ") and f["titulo"] == p:
                f["titulo"] = p[len(artista) + 3:]
    # tags extras do álbum: valor comum a todas as faixas, ou o mais frequente + "varia"
    extras = {}
    for k in sorted({k for f in faixas for k in f["extras"]}):
        vals = [tuple(f["extras"].get(k, ())) for f in faixas]
        comum = max(set(vals), key=vals.count)
        extras[k] = {"valores": list(comum), "varia": len(set(vals)) > 1}
    for f in faixas:
        del f["extras"]
    capa = acha_capa(pasta)
    outros = [a.name for a in pasta.rglob("*")
              if a.is_file() and a.suffix.lower() not in AUDIO and a != capa]
    return {
        "pasta": str(pasta.relative_to(DOWNLOADS)),
        "artista": artista or "",
        "album": album,
        "ano": ano,
        "mbid": mais_comum([f["mbid"] for f in faixas]),
        "capa": str(capa.relative_to(pasta)) if capa else "",
        "faixas": faixas,
        "extras": extras,
        "extras_mod": {},
        "outros": outros,
        "formatos": sorted({f["formato"] for f in faixas}),
        "sem_tags": sum(not f["tem_tags"] for f in faixas),
        "mtime": int(max((a.stat().st_mtime for a in arqs), default=0)),
    }


def destino(artista, album):
    """(pasta_do_album, já_existe) reaproveitando nomes de pastas equivalentes."""
    art_dir = acha_existente(ALBUMS, artista) or ALBUMS / limpa(artista)
    alb_dir = acha_existente(art_dir, album, norm_album) or art_dir / limpa(album)
    return alb_dir, alb_dir.is_dir()


def nome_faixa(f, multi, ext):
    n = f.get("faixa")
    if not n:
        return f"{limpa(f['titulo'])}{ext}"
    pref = f"{int(f.get('disco') or 1):02d}-{int(n):02d}" if multi else f"{int(n):02d}"
    return f"{pref} - {limpa(f['titulo'])}{ext}"


def plano(d):
    """Calcula nome final de cada faixa e avisos, sem tocar em nada."""
    artista, album = d["artista"].strip(), d["album"].strip()
    alb_dir, existe = destino(artista, album) if artista and album else (None, False)
    multi = len({int(f.get("disco") or 1) for f in d["faixas"]}) > 1
    existentes = {titulo_de_arquivo_lib(a) for a in alb_dir.iterdir()
                  if a.suffix.lower() in AUDIO} if existe else set()
    usados, saida = set(), []
    for f in d["faixas"]:
        ext = Path(f["arquivo"]).suffix.lower()
        nome = nome_faixa(f, multi, ext)
        estado = "ok"
        if alb_dir and (norm(f["titulo"]) in existentes or (alb_dir / nome).exists()):
            estado = "existe"
        elif nome in usados:
            estado = "repetida"
        usados.add(nome)
        saida.append({"arquivo": f["arquivo"], "destino": nome, "estado": estado})
    return {
        "destino": str(alb_dir.relative_to(MUSIC)) if alb_dir else "",
        "album_existe": existe,
        "faixas": saida,
        "problemas": [p for p, c in [
            ("Informe artista e álbum.", not artista or not album),
            ("Há faixas repetidas no destino.", any(s["estado"] == "repetida" for s in saida)),
        ] if c],
    }


# ---------- tags ----------

def escreve_tags(arq, d, f):
    m = mutagen.File(arq, easy=True)
    if m is None:
        raise RuntimeError(f"formato não suportado: {arq.name}")
    if m.tags is None:
        m.add_tags()
    t = m.tags
    t["title"] = f["titulo"]
    t["artist"] = f.get("artista") or d["artista"]
    t["albumartist"] = d["artista"]
    t["album"] = d["album"]
    if d.get("ano"):
        t["date"] = d["ano"]
    if f.get("faixa"):
        t["tracknumber"] = str(f["faixa"])
    t["discnumber"] = str(f.get("disco") or 1)
    if d.get("mbid"):
        try:
            t["musicbrainz_albumid"] = d["mbid"]
        except Exception:
            pass
    for k, vals in (d.get("extras_mod") or {}).items():
        if not CHAVE_OK.match(k) or k in GERENCIADAS:
            continue
        vals = [v for v in vals if v.strip()]
        try:
            if vals:
                t[k] = vals
            elif k in t:
                del t[k]
        except KeyError:   # chave que o formato não conhece: registra como campo livre
            if isinstance(t, EasyID3):
                EasyID3.RegisterTXXXKey(k, k.upper())
            elif isinstance(t, EasyMP4Tags):
                EasyMP4Tags.RegisterFreeformKey(k, k.upper())
            else:
                raise
            if vals:
                t[k] = vals
    m.save()


_mb_lock = threading.Lock()
_mb_ultimo = 0.0


def mb_get(url):
    """GET no MusicBrainz respeitando o limite de 1 req/s; repete se vier 503/429."""
    global _mb_ultimo
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for tentativa in range(4):
        with _mb_lock:
            espera = 1.1 - (time.monotonic() - _mb_ultimo)
            if espera > 0:
                time.sleep(espera)
            _mb_ultimo = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or tentativa == 3:
                abort(502, f"MusicBrainz respondeu {e.code}. Tente de novo em instantes.")
            time.sleep(2 * (tentativa + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            abort(502, f"Não consegui falar com o MusicBrainz: {getattr(e, 'reason', e)}")


def baixa_capa_caa(mbid, destino_arq):
    try:
        req = urllib.request.Request(f"https://coverartarchive.org/release/{mbid}/front-500",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            destino_arq.write_bytes(r.read())
        return True
    except Exception:
        return False


# ---------- API ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/favicon.svg")
def favicon():
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")


@app.get("/api/inbox")
def inbox():
    itens = []
    for pasta in sorted(p for p in DOWNLOADS.iterdir() if p.is_dir()):
        if not audio_files(pasta):
            continue
        d = rascunho(pasta)
        d["plano"] = plano(d)
        itens.append(d)
    itens.sort(key=lambda d: -d["mtime"])
    return jsonify(itens)


@app.post("/api/plano")
def api_plano():
    return jsonify(plano(request.get_json()))


@app.get("/api/capa")
def capa():
    pasta = seguro(request.args["pasta"])
    c = (pasta / request.args["arquivo"]).resolve()
    if pasta not in c.parents or c.suffix.lower() not in IMAGENS:
        abort(400)
    return send_from_directory(c.parent, c.name)


@app.get("/api/mb/busca")
def mb_busca():
    q = f'artist:"{request.args.get("artista", "")}" AND release:"{request.args.get("album", "")}"'
    url = "https://musicbrainz.org/ws/2/release?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": 25})
    rels = mb_get(url)["releases"]
    return jsonify([{
        "id": r["id"],
        "titulo": r["title"],
        "artista": "".join(c.get("name", "") + c.get("joinphrase", "") for c in r.get("artist-credit", [])),
        "data": r.get("date", ""),
        "pais": r.get("country", ""),
        "faixas": r.get("track-count"),
        "formato": "+".join(sorted({m.get("format") or "?" for m in r.get("media", [])})),
        "status": r.get("status", ""),
        "score": r.get("score"),
    } for r in rels])


@app.post("/api/mb/aplicar")
def mb_aplicar():
    """Busca o release e casa as faixas locais com as do MusicBrainz (por título, depois por posição)."""
    body = request.get_json()
    url = (f"https://musicbrainz.org/ws/2/release/{body['id']}?"
           + urllib.parse.urlencode({"inc": "recordings+artist-credits", "fmt": "json"}))
    rel = mb_get(url)
    mb = []
    for medium in rel["media"]:
        for t in medium["tracks"]:
            mb.append({"disco": medium["position"], "faixa": t["position"], "titulo": t["title"],
                       "artista": "".join(c.get("name", "") + c.get("joinphrase", "")
                                          for c in t.get("artist-credit", []))})
    locais = body["faixas"]
    livres = list(range(len(mb)))
    pares = {}
    # 1) título parecido
    cand = sorted(((difflib.SequenceMatcher(None, norm(l["titulo"]), norm(mb[j]["titulo"])).ratio(), i, j)
                   for i, l in enumerate(locais) for j in livres), reverse=True)
    for s, i, j in cand:
        if s < 0.6:
            break
        if i not in pares and j in livres:
            pares[i] = j
            livres.remove(j)
    # 2) o que sobrou: mesma posição (disco/faixa) se existir
    for i, l in enumerate(locais):
        if i in pares:
            continue
        for j in list(livres):
            if (mb[j]["disco"], mb[j]["faixa"]) == (l.get("disco") or 1, l.get("faixa")):
                pares[i] = j
                livres.remove(j)
                break
    artista = "".join(c.get("name", "") + c.get("joinphrase", "") for c in rel.get("artist-credit", []))
    return jsonify({
        "artista": artista,
        "album": rel["title"],
        "ano": (rel.get("date") or "")[:4],
        "mbid": rel["id"],
        "faixas": [({"titulo": mb[pares[i]]["titulo"], "faixa": mb[pares[i]]["faixa"],
                     "disco": mb[pares[i]]["disco"],
                     "artista": "" if mb[pares[i]]["artista"] == artista else mb[pares[i]]["artista"]}
                    if i in pares else None) for i in range(len(locais))],
        "sem_par_mb": [mb[j] for j in livres],
    })


def move_album(d):
    pasta = seguro(d["pasta"])
    p = plano(d)
    if p["problemas"]:
        return {"pasta": d["pasta"], "ok": False, "erro": " ".join(p["problemas"])}
    alb_dir = MUSIC / p["destino"]
    movidas, puladas, erros = [], [], []
    for f, s in zip(d["faixas"], p["faixas"]):
        if s["estado"] != "ok":
            puladas.append(f"{f['arquivo']} ({'já existe no destino' if s['estado'] == 'existe' else s['estado']})")
            continue
        origem = (pasta / f["arquivo"]).resolve()
        if pasta not in origem.parents or not origem.is_file():
            erros.append(f"{f['arquivo']}: não encontrado")
            continue
        try:
            escreve_tags(origem, d, f)
            alb_dir.mkdir(parents=True, exist_ok=True)
            alvo = alb_dir / s["destino"]
            shutil.move(str(origem), str(alvo))
            lrc = origem.with_suffix(".lrc")
            if lrc.exists() and not alvo.with_suffix(".lrc").exists():
                shutil.move(str(lrc), str(alvo.with_suffix(".lrc")))
            movidas.append(s["destino"])
        except Exception as e:
            erros.append(f"{f['arquivo']}: {e}")
    if movidas:
        capa = pasta / d["capa"] if d.get("capa") else None
        alvo_capa = alb_dir / "cover.jpg"
        if not any((alb_dir / n).exists() for n in ("cover.jpg", "cover.png", "folder.jpg")):
            if capa and capa.is_file():
                shutil.move(str(capa), str(alb_dir / ("cover" + capa.suffix.lower())))
            elif d.get("mbid"):
                baixa_capa_caa(d["mbid"], alvo_capa)
        info = alb_dir / ".albuminfo"
        if d.get("mbid") and not info.exists():
            info.write_text(json.dumps({"artist": d["artista"], "album": d["album"],
                                        "release_mbid": d["mbid"], "year": d.get("ano", "")},
                                       indent=2, ensure_ascii=False))
    # limpa a origem: só se não sobrou áudio; apaga apenas lixo conhecido
    if not audio_files(pasta):
        for a in list(pasta.rglob("*")):
            if a.is_file() and (a.suffix.lower() in LIXO or a.suffix.lower() in IMAGENS
                                or a.suffix.lower() == ".lrc"):
                a.unlink()
        for sub in sorted((x for x in pasta.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
            if not any(sub.iterdir()):
                sub.rmdir()
        if not any(pasta.iterdir()):
            pasta.rmdir()
    return {"pasta": d["pasta"], "ok": not erros, "destino": p["destino"],
            "movidas": len(movidas), "puladas": puladas, "erros": erros,
            "origem_removida": not pasta.exists()}


def album_biblioteca(rel):
    """Pasta de álbum pelo caminho relativo à biblioteca (Albums/Artista/Álbum)."""
    p = (MUSIC / rel).resolve()
    if ALBUMS not in p.parents or len(p.relative_to(ALBUMS).parts) != 2 or not p.is_dir():
        abort(400, "caminho inválido")
    return p


def compartilhaveis(pasta):
    return [a for a in pasta.rglob("*")
            if a.is_file() and not a.name.startswith(".")
            and a.suffix.lower() in AUDIO | IMAGENS | {".lrc"}]


def estado_share(rel):
    arqs = compartilhaveis(MUSIC / rel)
    n = sum((SHARES / a.relative_to(MUSIC)).exists() for a in arqs)
    return "sim" if n and n == len(arqs) else "parcial" if n else "nao"


def compartilha(rel):
    pasta = album_biblioteca(rel)
    novos = 0
    for a in compartilhaveis(pasta):
        alvo = SHARES / a.relative_to(MUSIC)
        if alvo.exists():
            continue
        alvo.parent.mkdir(parents=True, exist_ok=True)
        os.link(a, alvo)
        novos += 1
    return novos


def descompartilha(rel):
    pasta = album_biblioteca(rel)
    alvo = SHARES / pasta.relative_to(MUSIC)
    if not alvo.is_dir():
        return 0
    removidos = 0
    for a in alvo.rglob("*"):
        # só apaga hardlinks; arquivo que não é cópia da biblioteca fica
        if a.is_file() and a.stat().st_nlink > 1:
            a.unlink()
            removidos += 1
    for sub in sorted((x for x in alvo.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
        if not any(sub.iterdir()):
            sub.rmdir()
    for d in (alvo, alvo.parent, alvo.parent.parent):
        if d != SHARES and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return removidos


@app.get("/api/biblioteca")
def biblioteca():
    itens = []
    for alb in sorted(ALBUMS.glob("*/*")):
        if alb.is_dir():
            rel = str(alb.relative_to(MUSIC))
            itens.append({"rel": rel, "artista": alb.parent.name, "album": alb.name,
                          "faixas": len(audio_files(alb)), "share": estado_share(rel)})
    return jsonify(itens)


@app.post("/api/compartilhar")
def api_compartilhar():
    d = request.get_json()
    fn = compartilha if d.get("ligar", True) else descompartilha
    return jsonify({"resultados": [{"rel": r, "arquivos": fn(r), "share": estado_share(r)}
                                   for r in d["albuns"]]})


@app.post("/api/mover")
def mover():
    dados = request.get_json()
    resultados = [move_album(d) for d in dados["albuns"]]
    if dados.get("compartilhar"):
        for r in resultados:
            if r.get("movidas"):
                try:
                    compartilha(r["destino"])
                    r["compartilhado"] = True
                except Exception as e:
                    r["erros"].append(f"compartilhar: {e}")
                    r["ok"] = False
    return jsonify({"resultados": resultados})


@app.post("/api/descartar")
def descartar():
    pasta = seguro(request.get_json()["pasta"])
    shutil.rmtree(pasta)
    return jsonify({"ok": True})
