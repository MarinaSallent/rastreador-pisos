#!/usr/bin/env python3
"""
Descubre automáticamente la web y la página de alquileres de las agencias de agencias.csv.

Para cada agencia (agrupando las oficinas de una misma marca, p. ej. todas las Tecnocasa):
  1. Busca la marca en un buscador (DuckDuckGo y, si falla, Bing; o Brave si hay BRAVE_API_KEY).
  2. Descarta portales y directorios (Idealista, Fotocasa, redes sociales...) y elige el dominio
     que mejor coincide con el nombre de la agencia.
  3. Abre la web y comprueba que es una inmobiliaria.
  4. Busca su página de alquileres y prueba si el rastreador detecta anuncios en ella.
  5. Si los detecta, rellena url_alquiler y marca vigilar = sí.

Guarda el progreso en datos/descubrimiento.json, así que puede ejecutarse por tandas
(lo hace la tarea "Descubrir webs" de GitHub cada hora hasta terminar).

Uso:
  python descubrir.py --max 400          # procesa hasta 400 marcas pendientes
  python descubrir.py --aplicar          # solo vuelca lo descubierto en agencias.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import rastreador as r

RAIZ = Path(__file__).resolve().parent
CSV = RAIZ / "agencias.csv"
PROGRESO = RAIZ / "datos" / "descubrimiento.json"

# Dominios que nunca son la web propia de una agencia
NO_SON_AGENCIA = re.compile(
    r"(idealista|fotocasa|habitaclia|pisos\.com|yaencontre|milanuncios|wallapop|enalquiler|tucasa\.com|"
    r"trovit|nestoria|thinkspain|kyero|spotahome|uniplaces|badi|housinganywhere|"
    r"facebook|instagram|twitter|x\.com|linkedin|youtube|tiktok|pinterest|google|goo\.gl|bing|duckduckgo|"
    r"wikipedia|paginasamarillas|qdq|cylex|infoempresa|einforma|axesor|empresite|infocif|librebor|"
    r"iberinform|datoscif|expansion\.com|elconfidencial|lavanguardia|elperiodico|ara\.cat|"
    r"aicat|apibcn|api\.cat|gencat|agenciahabitatge|tripadvisor|yelp|foursquare|hotfrog|"
    r"guiaempresa|todoinmobiliarias|inmobiliarias\.info|tuslistas|educaweb|glassdoor|indeed|"
    r"infojobs|jobtoday|trustpilot|houzz|vivanuncios|listado|directorio|mapcarta|waze|apple\.com)",
    re.I,
)
PALABRAS_VACIAS = {"sl", "slu", "scp", "sa", "sau", "slp", "sccl", "scsp", "s", "l", "u", "de", "del", "la", "el",
                   "les", "los", "las", "i", "y", "and", "the", "bcn", "barcelona", "fincas", "finques",
                   "inmobiliaria", "immobiliaria", "inmobiliario", "immobiliari", "real", "estate", "grup",
                   "grupo", "group", "serveis", "servicios", "gestio", "gestion", "assessors", "asesores",
                   "consulting", "properties", "propiedades", "home", "homes", "house", "casa", "agency",
                   "agencia", "cb", "invest", "inversiones"}
PATRON_ALQUILER = re.compile(r"(alquil|lloguer|llogar|rent|arrend|location|to-let|let\b)", re.I)
PATRON_VENTA = re.compile(r"(venta|venda|vendre|comprar|sale|buy|vender|traspas)", re.I)

UA = r.UA
SES = requests.Session()
SES.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,ca;q=0.8"})


# ---------------------------------------------------------------- nombres y marcas
def nombre_comercial(nombre: str) -> str:
    m = re.search(r"\(([^()]*(?:\([^()]*\))?[^()]*)\)\s*$", nombre)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return nombre.strip()


def es_persona(nombre: str) -> bool:
    """'Apellido Apellido, Nombre' sin marca comercial: casi nunca tiene web propia."""
    return "(" not in nombre and "," in nombre and not re.search(r"\b(sl|slu|scp|sa|sau|slp)\b", nombre, re.I)


def tokens(texto: str) -> list[str]:
    t = re.sub(r"[^a-z0-9 ]", " ", r.normalizar(texto).replace("&", " "))
    return [w for w in t.split() if len(w) > 1 and w not in PALABRAS_VACIAS]


def tokens_marca(marca: str) -> list[str]:
    """Palabras distintivas de la marca; si todas son genéricas ("La Casa Agency"), se usan todas."""
    return tokens(marca) or [w for w in re.sub(r"[^a-z0-9 ]", " ", r.normalizar(marca)).split()
                             if len(w) > 1 and w not in {"sl", "slu", "scp", "sa", "de", "la", "el", "i", "y"}]


def clave_marca(nombre: str) -> str:
    return " ".join(tokens_marca(nombre_comercial(nombre))) or r.normalizar(nombre)


# ---------------------------------------------------------------- buscadores
def _pausa():
    time.sleep(random.uniform(2.5, 5.0))


def buscar_ddg(q: str) -> list[tuple[str, str]]:
    resp = SES.post("https://html.duckduckgo.com/html/", data={"q": q, "kl": "es-es"}, timeout=20)
    if resp.status_code != 200 or "result__a" not in resp.text:
        raise RuntimeError(f"ddg {resp.status_code}")
    return parsear_ddg(resp.text)


def parsear_ddg(html: str) -> list[tuple[str, str]]:
    out = []
    for a in BeautifulSoup(html, "html.parser").select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if href.startswith("http"):
            out.append((href, a.get_text(" ", strip=True)))
    return out


def buscar_bing(q: str) -> list[tuple[str, str]]:
    resp = SES.get(f"https://www.bing.com/search?q={quote_plus(q)}&setlang=es&cc=ES", timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"bing {resp.status_code}")
    return parsear_bing(resp.text)


def parsear_bing(html: str) -> list[tuple[str, str]]:
    out = []
    for a in BeautifulSoup(html, "html.parser").select("li.b_algo h2 a"):
        href = a.get("href", "")
        if "bing.com/ck/" in href:  # enlace de seguimiento: la URL real va codificada en el parámetro u
            u = parse_qs(urlparse(href).query).get("u", [""])[0]
            if u.startswith("a1"):
                import base64
                try:
                    href = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
                except Exception:
                    continue
        if href.startswith("http"):
            out.append((href, a.get_text(" ", strip=True)))
    return out


def buscar_brave(q: str) -> list[tuple[str, str]]:
    resp = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "country": "es"},
                        headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
                        timeout=20)
    resp.raise_for_status()
    return [(x["url"], x.get("title", "")) for x in resp.json().get("web", {}).get("results", [])]


MOTORES_CAIDOS: dict[str, float] = {}


def buscar(q: str) -> list[tuple[str, str]]:
    motores = [("brave", buscar_brave)] if os.environ.get("BRAVE_API_KEY") else []
    motores += [("ddg", buscar_ddg), ("bing", buscar_bing)]
    for nombre, f in motores:
        if MOTORES_CAIDOS.get(nombre, 0) > time.time():
            continue
        try:
            res = f(q)
            _pausa()
            return res
        except Exception as e:
            print(f"   buscador {nombre} falla ({e}); pruebo otro", file=sys.stderr)
            MOTORES_CAIDOS[nombre] = time.time() + 600  # lo apartamos 10 minutos
    raise RuntimeError("todos los buscadores bloqueados")


# ---------------------------------------------------------------- elegir y verificar la web
def dominio(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def puntuar(url: str, titulo: str, marca_tokens: list[str]) -> float:
    if not marca_tokens:
        return 0
    dom = re.sub(r"[^a-z0-9]", "", dominio(url).rsplit(".", 1)[0])
    tit = " ".join(tokens(titulo))
    en_dominio = sum(1 for t in marca_tokens if t in dom) / len(marca_tokens)
    en_titulo = sum(1 for t in marca_tokens if t in tit) / len(marca_tokens)
    junto = 1.0 if "".join(marca_tokens) in dom else 0.0
    return max(en_dominio, junto) * 2 + en_titulo


def elegir_web(resultados: list[tuple[str, str]], marca: str) -> tuple[str | None, float]:
    mt = tokens_marca(marca)
    mejor, puntos = None, 0.0
    for url, titulo in resultados[:10]:
        if NO_SON_AGENCIA.search(dominio(url)):
            continue
        p = puntuar(url, titulo, mt)
        if p > puntos:
            mejor, puntos = url, p
    if mejor and puntos >= 1.0:
        u = urlparse(mejor)
        return f"{u.scheme}://{u.netloc}/", puntos
    return None, puntos


def descargar(url: str) -> tuple[str, str]:
    resp = SES.get(url, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text, resp.url


def parece_inmobiliaria(html: str, marca: str, aicat: set[str]) -> bool:
    t = r.normalizar(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))[:200000]
    sector = re.search(r"(inmobiliari|immobiliari|finques|fincas|alquiler|lloguer|venta|venda|pisos|real estate|"
                       r"propert|aicat|api\b)", t)
    nombre = any(tk in t for tk in tokens_marca(marca)) or any(a in t for a in aicat)
    return bool(sector and nombre)


def paginas_alquiler(html: str, base: str) -> list[str]:
    """Enlaces de la propia web que parecen el listado de alquileres, mejores primero."""
    soup = BeautifulSoup(html, "html.parser")
    cand: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"].strip())
        if not url.startswith("http") or not r.mismo_sitio(url, base):
            continue
        texto = r.normalizar(a.get_text(" ", strip=True) + " " + url)
        if not PATRON_ALQUILER.search(texto) or PATRON_VENTA.search(r.normalizar(a.get_text(" ", strip=True))):
            continue
        puntos = 1
        if re.search(r"(alquiler|lloguer)", texto):
            puntos += 2
        if re.search(r"(piso|pis|vivienda|habitatge|apartament|flat)", texto):
            puntos += 1
        if "barcelona" in texto:
            puntos += 1
        if re.search(r"\d{3,}", urlparse(url).path):  # parece ficha de un piso, no listado
            puntos -= 2
        cand[url.split("#")[0]] = max(cand.get(url.split("#")[0], 0), puntos)
    return [u for u, _ in sorted(cand.items(), key=lambda x: -x[1])][:4]


def descubrir_marca(marca: str, municipio: str, aicat: set[str]) -> dict:
    q = f'"{marca}" inmobiliaria {municipio}'
    res = buscar(q)
    web, puntos = elegir_web(res, marca)
    info = {"marca": marca, "consulta": q, "candidatas": [u for u, _ in res[:5]], "puntos": round(puntos, 2)}
    if not web:
        return {**info, "estado": "sin_web"}
    try:
        html, final = descargar(web)
    except Exception as e:
        return {**info, "estado": "web_no_responde", "web": web, "error": str(e)[:120]}
    if not parece_inmobiliaria(html, marca, aicat):
        return {**info, "estado": "web_dudosa", "web": web}
    info.update(web=final, estado="web_ok")
    for url in paginas_alquiler(html, final):
        try:
            h2, url_final = descargar(url)
            anuncios = r.extraer_anuncios(h2, url_final)
        except Exception:
            continue
        if anuncios:
            return {**info, "url_alquiler": url_final, "anuncios": len(anuncios), "estado": "vigilable"}
        time.sleep(1)
    return info


# ---------------------------------------------------------------- CSV
def cargar_csv() -> tuple[list[str], list[dict]]:
    with CSV.open(encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        return lector.fieldnames, list(lector)


def guardar_csv(campos: list[str], filas: list[dict]) -> None:
    with CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(filas)


def aplicar(progreso: dict) -> tuple[int, int]:
    campos, filas = cargar_csv()
    webs = vig = 0
    for fila in filas:
        res = progreso.get(clave_marca(fila["nombre"]))
        if not res:
            continue
        if res.get("web") and res["estado"] in ("web_ok", "vigilable") and not fila.get("web"):
            fila["web"] = res["web"]
        if res.get("url_alquiler") and not fila.get("url_alquiler"):
            fila["url_alquiler"] = res["url_alquiler"]
            if not fila.get("vigilar"):
                fila["vigilar"] = "sí"
        webs += bool(fila.get("web"))
        vig += r.normalizar(fila.get("vigilar", "")) in r.SI and bool(fila.get("url_alquiler"))
    guardar_csv(campos, filas)
    return webs, vig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=400, help="marcas a procesar en esta tanda")
    ap.add_argument("--minutos", type=int, default=50, help="tiempo máximo de la tanda")
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--incluir-personas", action="store_true",
                    help="buscar también agentes sin nombre comercial (suelen no tener web)")
    args = ap.parse_args()

    progreso = r.cargar_json(PROGRESO, {})
    if args.aplicar:
        print("Webs: %d · vigiladas: %d" % aplicar(progreso))
        return 0

    _, filas = cargar_csv()
    marcas: dict[str, dict] = {}
    for f in filas:
        if f.get("web") or f.get("url_alquiler"):
            continue
        if es_persona(f["nombre"]) and not args.incluir_personas:
            continue
        k = clave_marca(f["nombre"])
        m = marcas.setdefault(k, {"marca": nombre_comercial(f["nombre"]), "municipio": f.get("municipio") or "Barcelona",
                                  "aicat": set(), "tu_zona": False})
        m["aicat"].add(f["aicat"])
        m["tu_zona"] |= r.normalizar(f.get("en_tus_zonas", "")) in r.SI
    pendientes = [(k, m) for k, m in marcas.items() if k not in progreso]
    pendientes.sort(key=lambda x: (not x[1]["tu_zona"], -len(x[1]["aicat"])))  # primero tus barrios y las cadenas
    print(f"{len(marcas)} marcas · {len(pendientes)} pendientes · esta tanda: {min(args.max, len(pendientes))}")

    fin = time.time() + args.minutos * 60
    hechas = 0
    for k, m in pendientes[: args.max]:
        if time.time() > fin:
            break
        try:
            res = descubrir_marca(m["marca"], m["municipio"], m["aicat"])
        except RuntimeError as e:
            print(f"Parando la tanda: {e}")
            break
        except Exception as e:
            res = {"marca": m["marca"], "estado": "error", "error": str(e)[:150]}
        progreso[k] = res
        hechas += 1
        print(f"[{res['estado']:>15}] {m['marca'][:45]:45} {res.get('url_alquiler') or res.get('web') or ''}")
        if hechas % 20 == 0:
            r.guardar_json(PROGRESO, progreso)
    r.guardar_json(PROGRESO, progreso)
    webs, vig = aplicar(progreso)
    restantes = len(pendientes) - hechas
    print(f"Tanda: {hechas} marcas · quedan {restantes} · agencias con web: {webs} · vigiladas: {vig}")
    Path(os.environ.get("GITHUB_OUTPUT", "/dev/null")).open("a").write(f"restantes={restantes}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
