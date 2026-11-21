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


GENERICAS_DOMINIO = PALABRAS_VACIAS | {"immobiliaris", "inmobiliarios", "immobiliaries", "inmobiliarias", "apartments",
                                       "apartamentos", "flats", "pisos", "habitat", "llar", "rent", "lloguer",
                                       "alquiler", "assessoria", "asesoria", "administracio", "administracion",
                                       "patrimonial", "patrimonio", "consultors", "consultores", "services"}


def dominio_de_marca(url: str, marca: str) -> bool:
    """¿El dominio lleva el nombre de la agencia? Evita quedarse con webs de otros (finanzio.es para
    «Hipotecasolución», serveisimmobiliaris.cat para «Serveis Immobiliaris Sarrià»…)."""
    host = re.sub(r"^www\d?\.", "", urlparse(url).netloc.lower().split(":")[0])
    host = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0])
    palabras = re.sub(r"[^a-z0-9 ]", " ", r.normalizar(marca).replace("&", " ")).split()
    propias = [w for w in palabras if len(w) >= 2 and w not in GENERICAS_DOMINIO]
    if propias:
        return any(w in host for w in propias)
    completo = "".join(w for w in palabras if w not in LEGALES_DOMINIO)
    return bool(completo) and completo in host


LEGALES_DOMINIO = {"sl", "slu", "scp", "sa", "sau", "slp", "sccl", "scsp", "cb"}


# ---------------------------------------------------------------- buscadores
def _pausa():
    time.sleep(random.uniform(2.5, 5.0))


def parsear_generico(html: str, motor: str) -> list[tuple[str, str]]:
    """Enlaces externos de una página de resultados (válido para cualquier buscador)."""
    out, vistos = [], set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"]
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if not href.startswith("http") or motor in dominio(href):
            continue
        d = dominio(href)
        if d in vistos or re.search(r"(microsoft|msn|bing|duckduckgo|mojeek|go\.microsoft)", d):
            continue
        texto = a.get_text(" ", strip=True)
        if len(texto) < 3:
            continue
        vistos.add(d)
        out.append((href, texto))
    return out


def buscar_mojeek(q: str) -> list[tuple[str, str]]:
    resp = SES.get(f"https://www.mojeek.com/search?q={quote_plus(q)}&lb=es", timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"mojeek {resp.status_code}")
    return parsear_generico(resp.text, "mojeek")


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
    resp = SES.get(f"https://www.bing.com/search?q={quote_plus(q)}&setlang=es&cc=ES", timeout=20,
                   cookies={"SRCHHPGUSR": "ADLT=OFF", "BCP": "AD=1&AL=1&SM=1"})
    if resp.status_code != 200:
        raise RuntimeError(f"bing {resp.status_code}")
    return parsear_bing(resp.text) or parsear_generico(resp.text, "bing")


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
SIN_BUSCADOR = {"activo": False}


def buscar(q: str) -> list[tuple[str, str]]:
    motores = [("brave", buscar_brave)] if os.environ.get("BRAVE_API_KEY") else []
    motores += [("mojeek", buscar_mojeek), ("bing", buscar_bing), ("ddg", buscar_ddg)]
    for nombre, f in motores:
        if MOTORES_CAIDOS.get(nombre, 0) > time.time():
            continue
        try:
            res = f(q)
            _pausa()
            if not res:  # una página sin ningún resultado suele ser un bloqueo, no «no existe»
                raise RuntimeError("0 resultados")
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
    sector = set(re.findall(r"(inmobiliari|immobiliari|finques|fincas|alquiler|lloguer|pisos|habitatge|vivienda|"
                            r"real estate|propiedades|aicat|api\b)", t))
    nombre = any(tk in t for tk in tokens_marca(marca)) or any(a in t for a in aicat)
    espana = re.search(r"(barcelona|catalunya|cataluna|espana|madrid|valencia)", t)
    return bool(len(sector) >= 2 and nombre and espana)


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


LEGALES = {"sl", "slu", "scp", "sa", "sau", "slp", "sccl", "scsp", "cb", "s", "l", "u"}


def dominios_probables(marca: str) -> list[str]:
    """galafinques.com, gala-finques.es, tecnocasa.es… a partir del nombre comercial."""
    palabras = [w for w in re.sub(r"[^a-z0-9 ]", " ", r.normalizar(marca).replace("&", " ")).split()
                if w not in LEGALES]
    con_articulos = palabras
    palabras = [w for w in palabras if w not in {"de", "del", "la", "el", "les", "i", "y"}] or palabras
    if not palabras or len("".join(palabras)) < 4:
        return []
    bases = ["".join(palabras), "-".join(palabras)]
    if con_articulos != palabras:
        bases.insert(0, "".join(con_articulos))
    if len(palabras) > 1 and palabras[0] in {"fincas", "finques", "inmobiliaria", "immobiliaria"}:
        bases.append("".join(palabras[1:] + palabras[:1]))
    if len(palabras) > 1 and palabras[-1] in {"fincas", "finques", "inmobiliaria", "immobiliaria"}:
        bases.append("".join(palabras[-1:] + palabras[:-1]))
    if len(palabras) > 2:
        bases.append("".join(palabras[:2]))
    distintivas = {w for w in palabras if w not in GENERICAS_DOMINIO}
    if distintivas:  # «serveisimmobiliaris.cat» es de cualquiera: la base debe llevar alguna palabra propia
        bases = [b for b in bases if any(w in b for w in distintivas)]
    vistos, out = set(), []
    for b in bases:
        for tld in ("es", "cat", "com", "net"):
            d = f"{b}.{tld}"
            if d not in vistos and len(b) <= 40:
                vistos.add(d)
                out.append(f"https://www.{d}/")
    return out


def adivinar_web(marca: str, aicat: set[str]) -> tuple[str, str] | None:
    """Prueba dominios probables en paralelo y devuelve (url, html) del que sea la agencia."""
    from concurrent.futures import ThreadPoolExecutor

    def probar(url):
        try:
            resp = SES.get(url, timeout=8, allow_redirects=True)
            if resp.status_code == 200 and len(resp.text) > 500:
                if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = resp.apparent_encoding
                return url, resp.url, resp.text
        except Exception:
            pass
        return None

    candidatos = dominios_probables(marca)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(probar, candidatos):
            if (res and not NO_SON_AGENCIA.search(dominio(res[1])) and dominio_de_marca(res[1], marca)
                    and parece_inmobiliaria(res[2], marca, aicat)):
                return res[1], res[2]
    return None


VERSION = 3  # sube cuando cambian los criterios: las marcas de versiones anteriores se revisan de nuevo

ZONA_BCN = re.compile(r"(barcelona|bcn|hospitalet|badalona|sant cugat|esplugues|cornella|santa coloma|sant adria|"
                      r"eixample|gracia|sarria|sant gervasi|sant marti|poblenou|sant andreu|les corts|sants|horta|"
                      r"nou barris|ciutat vella|sagrera|clot|guinardo|vallcarca|putxet|bonanova|\b08\d{3}\b)")
RUTA_PROHIBIDA = re.compile(r"(noticia|news|blog|actualitat|article|servici|services|servei|gestion|gestio\b|gestio-|administraci|"
                            r"administrador|tasaci|valoraci|relocation|local|oficina|despacho|nave|industrial|parking|"
                            r"garaje|trastero|obra.?nueva|rentabilidad|inversi|por-que|faq|contact|vacacional|turistic|"
                            r"temporada|habitacion|room|coliving|holiday|moratoria|guia|guide|consejo|consell|"
                            r"como-|que-es|leasing|build.?rent|build.?to.?rent|project|proyecto|sell|vender|vendre|"
                            r"agencia-immobiliaria|quienes-somos|nosotros|about|limitacion|ley-|llei-)")
RUTA_VENTA = re.compile(r"(venta|venda|comprar|buy|sale|traspas)")
RUTAS_TIPICAS = ["alquiler", "es/alquiler", "lloguer", "ca/lloguer", "alquiler/es/", "lloguer/ca/", "inmuebles/alquiler",
                 "propiedades/alquiler", "es/inmuebles-en-alquiler", "pisos-alquiler", "es/pisos-en-alquiler",
                 "rent", "en/rent"]
RUTA_ALQUILER = re.compile(r"(?<![a-z])(alquil\w*|lloguer\w*|llogu\w*|arrend\w*|rent|rental|rentals|renting|"
                           r"for-rent|to-rent|status-alquiler)(?![a-z])")
NO_ES_PAGINA = re.compile(r"(\.(png|jpe?g|gif|webp|svg|pdf|docx?|xlsx?|zip)$|/wp-content/uploads/)")
FICHA_RUTA = re.compile(r"(/ficha/|/detalle|/details?/|/property/[^/]+|/propiedad/[^/]+|/inmueble/[^/]+|"
                        r"/immoble/[^/]+|/mcpropiedades/[^/]+|/anuncio/[^/]+|/listing/[^/]+|inmueble\.php|"
                        r"ficha\.php|detalle\.php|[?&](id|ref|idinmueble|cod)=\d+)")
UN_PISO = re.compile(r"\b(piso|pis|casa|atico|chalet|apartamento|apartament|duplex|estudio|estudi|loft|torre)\b")
VARIOS = re.compile(r"\b(pisos|casas|cases|viviendas|habitatges|inmuebles|immobles|propiedades|apartamentos|"
                    r"apartaments|aticos|listado|resultados|buscador|search)\b")
OTRA_CIUDAD = re.compile(r"(?<![a-z])(madrid|valencia|sevilla|malaga|alicante|badajoz|girona|tarragona|lleida|tortosa|"
                         r"murcia|zaragoza|bilbao|mallorca|palma|ibiza|eivissa|menorca|marbella|cadiz|granada|"
                         r"asturias|oviedo|cantabria|santander|coruna|vigo|toledo|valladolid|salamanca|cordoba|"
                         r"almeria|huelva|jaen|castellon|tenerife|canarias|gran-canaria|lanzarote|reus|salou|"
                         r"cambrils|figueres|blanes|lloret|andorra|javea|benidorm|torrevieja|denia|costa-brava|"
                         r"cardedeu|ripollet|manresa|vic|igualada|vilanova|sitges|mataro)(?![a-z])")
FECHA_BLOG = re.compile(r"/(19|20)\d\d/\d\d?/")


def validar_ruta(url: str, anuncios: int | None = None) -> tuple[bool, str]:
    """Comprobaciones que solo miran la dirección. Con anuncios == 0 (página vacía hoy) se exige que la propia
    dirección diga «alquiler», porque no hay pisos con los que confirmarlo."""
    p = urlparse(url)
    ruta = r.normalizar(p.path + ("?" + p.query if p.query else ""))
    ruta_sep = re.sub(r"[^a-z0-9]+", "-", ruta)
    if NO_ES_PAGINA.search(p.path.lower()):
        return False, "es una imagen o un documento, no una página"
    if p.path.strip("/") == "" and not RUTA_ALQUILER.search(ruta_sep):
        return False, "es la portada"
    if FECHA_BLOG.search(p.path) or RUTA_PROHIBIDA.search(ruta):
        return False, "la dirección es de noticias, servicios, locales u oficinas"
    if RUTA_VENTA.search(ruta) and not RUTA_ALQUILER.search(ruta_sep):
        return False, "la dirección es de venta"
    if OTRA_CIUDAD.search(ruta_sep) and "barcelona" not in ruta:
        return False, "la página es de otra ciudad"
    segmentos = [s for s in p.path.lower().split("/") if s]
    ultimo = segmentos[-1] if segmentos else ""
    if (FICHA_RUTA.search(p.path.lower() + ("?" + p.query.lower() if p.query else ""))
            or any(re.fullmatch(r"\d{3,}", s) or re.search(r"\d{4,}", s) for s in segmentos)
            or ultimo.count("-") >= 7):
        return False, "parece la ficha de un solo piso"
    palabras_ultimo = ultimo.replace("-", " ").replace("_", " ")
    if ultimo.count("-") >= 3 and UN_PISO.search(palabras_ultimo) and not VARIOS.search(palabras_ultimo):
        return False, "parece la ficha de un solo piso"
    if anuncios == 0 and not RUTA_ALQUILER.search(ruta_sep):
        return False, "está vacía y la dirección no dice que sea de alquiler"
    return True, "ok"


NO_ENCONTRADA = re.compile(r"(404|no encontrad|not found|no existe|pagina no disponible|page not found|no s'ha trobat)")


def validar_listado(html: str, url: str) -> tuple[bool, str, int]:
    """¿Es la página donde la agencia publica sus pisos de alquiler? Puede estar vacía hoy: también vale.

    Se descartan direcciones que claramente no lo son (noticias, servicios, locales, venta, la ficha de un solo
    piso, imágenes, otras ciudades) y listados con anuncios pero todos de fuera de la zona de Barcelona."""
    ok, motivo = validar_ruta(url)
    if not ok:
        return False, motivo, 0
    soup = BeautifulSoup(html, "html.parser")
    texto = r.normalizar(soup.get_text(" ", strip=True)[:20000])
    titulo = r.normalizar((soup.title.get_text() if soup.title else "") or "")
    if NO_ENCONTRADA.search(titulo) or (len(texto) < 3000 and NO_ENCONTRADA.search(texto)):
        return False, "la página no existe", 0
    ruta = r.normalizar(urlparse(url).path + "?" + urlparse(url).query)
    if not re.search(r"(alquil|lloguer|llogar|rent|arrend)", ruta + " " + titulo + " " + texto[:5000]):
        return False, "no habla de alquiler", 0
    anuncios = r.extraer_anuncios(html, url)
    if anuncios:
        precios = [r.extraer_precio(a["texto"]) for a in anuncios]
        if any(precios) and not any(p and 250 <= p <= 8000 for p in precios):
            return False, "los anuncios no tienen precios de alquiler", len(anuncios)
        textos = r.normalizar(" ".join(a["texto"] + " " + a["url"] for a in anuncios) + " " + url)
        if not ZONA_BCN.search(textos) and (len(anuncios) >= 3 or OTRA_CIUDAD.search(textos)):
            return False, "ningún anuncio de la zona de Barcelona", len(anuncios)
    else:
        ok, motivo = validar_ruta(url, 0)
        if not ok:
            return False, motivo, 0
    return True, "ok", len(anuncios)


PERFIL_PORTAL = re.compile(
    r"https?://(?:www\.)?(idealista\.com/(?:[a-z]{2}/)?pro/[^/?#\s\"'<>]+(?:/[^?#\s\"'<>]*)?|"
    r"fotocasa\.es/[^\s\"'#]*(?:inmobiliaria|agencia|profesional)[^\s\"'#]*|"
    r"habitaclia\.com/[^\s\"'#]*(?:inmobiliaria|agencia|profesional|-i\d+)[^\s\"'#]*|"
    r"pisos\.com/inmobiliaria[^\s\"'#]*|yaencontre\.com/[^\s\"'#]*(?:inmobiliaria|agencia)[^\s\"'#]*)", re.I)


def enlace_portal(html: str) -> str | None:
    """Si la web de la agencia manda a su escaparate en Idealista (o Fotocasa, Habitaclia…), lo devuelve.
    Prefiere el enlace a los alquileres del perfil."""
    encontrados = [m.group(0).rstrip("\\") for m in PERFIL_PORTAL.finditer(html or "")]
    if not encontrados:
        return None
    encontrados.sort(key=lambda u: (not re.search(r"(alquiler|lloguer|rent)", u), "idealista" not in u, len(u)))
    return encontrados[0]


def buscar_paginas_alquiler(info: dict, html: str, web: str) -> dict:
    info["version"] = VERSION
    portal = enlace_portal(html)
    if portal:
        info["portal"] = portal
    paginas = paginas_alquiler(html, web)
    if not paginas:  # el menú de la portada a veces se pinta con JavaScript
        try:
            html_js = r.descargar_js(web, 30)
            paginas = paginas_alquiler(html_js, web)
        except Exception:
            pass
    base = web if web.endswith("/") else web + "/"
    for ruta in RUTAS_TIPICAS:  # direcciones habituales aunque no estén enlazadas en la portada
        u = urljoin(base, ruta)
        if u not in paginas:
            paginas.append(u)
    rechazos = {}
    vacias: list[str] = []  # páginas de alquiler válidas pero hoy sin anuncios legibles
    for url in paginas:
        try:
            h2, url_final = descargar(url)
        except Exception:
            continue
        if url_final in vacias or url_final in rechazos:
            continue
        ok, motivo, n = validar_listado(h2, url_final)
        if ok and n:
            return {**info, "url_alquiler": url_final, "anuncios": n, "estado": "vigilable"}
        if ok:
            vacias.append(url_final)
        else:
            rechazos[url_final] = motivo
        time.sleep(0.5)
    # ¿Las «vacías» cargan los pisos con JavaScript? Se prueba con un navegador real
    for url in vacias[:2]:
        try:
            ok, motivo, n = validar_listado(r.descargar_js(url, 30), url)
        except Exception:
            continue
        if ok and n:
            return {**info, "url_alquiler": url, "anuncios": n, "estado": "vigilable", "modo": "js"}
    if not info.get("portal"):  # el menú pintado con JavaScript también puede llevar el enlace al portal
        try:
            info["portal"] = enlace_portal(r.descargar_js(web, 30)) or None
        except Exception:
            pass
        if not info.get("portal"):
            info.pop("portal", None)
    if info.get("portal"):  # sus pisos están en Idealista u otro portal, que bloquea al bot
        return {**info, "estado": "publica_en_portal"}
    if vacias:  # sin pisos hoy: se vigila igual; el rastreador la aparta si en 6 meses no publica nada
        return {**info, "url_alquiler": vacias[0], "anuncios": 0, "estado": "vigilable"}
    info["rechazadas"] = dict(list(rechazos.items())[:5])
    info["revisado_js"] = True
    return info


def descubrir_marca(marca: str, municipio: str, aicat: set[str], web_conocida: str | None = None) -> dict:
    if web_conocida:  # ya sabemos la web: solo hay que buscar bien la página de alquileres
        try:
            html, final = descargar(web_conocida)
            return buscar_paginas_alquiler({"marca": marca, "metodo": "dominio", "web": final, "estado": "web_ok"},
                                           html, final)
        except Exception:
            pass
    # 1) Sin buscador: probar dominios con el nombre de la agencia
    adivinada = adivinar_web(marca, aicat)
    if adivinada:
        web, html = adivinada
        info = {"marca": marca, "metodo": "dominio", "web": web, "estado": "web_ok"}
        return buscar_paginas_alquiler(info, html, web)
    # 2) Buscador (si hoy están bloqueados, se deja para otra tanda)
    if SIN_BUSCADOR["activo"]:
        return {"marca": marca, "estado": "busqueda_pendiente"}
    q = f'"{marca}" inmobiliaria {municipio}'
    try:
        res = buscar(q)
    except RuntimeError:
        SIN_BUSCADOR["activo"] = True
        print("   Buscadores bloqueados: sigo solo probando dominios", file=sys.stderr)
        return {"marca": marca, "estado": "busqueda_pendiente"}
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
    info.update(web=final, estado="web_ok", metodo="buscador")
    return buscar_paginas_alquiler(info, html, final)


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
    """Vuelca lo descubierto en agencias.csv. Tus cambios a mano van en mis_agencias.csv y mandan sobre esto."""
    campos, filas = cargar_csv()
    webs = vig = portales = 0
    for fila in filas:
        res = progreso.get(clave_marca(fila["nombre"]))
        if res and res.get("revisar"):  # pendiente de revisar con los criterios nuevos: de momento no se vigila
            fila["url_alquiler"] = fila["vigilar"] = fila["modo"] = ""
        elif res:
            fila["web"] = res.get("web", "") if res["estado"] in ("web_ok", "vigilable", "publica_en_portal") else ""
            nota = fila.get("notas", "")
            if res.get("portal") and res["estado"] == "publica_en_portal":
                nombre_portal = r.portal_de(res["portal"]) or "un portal"
                if not nota or nota.startswith("Publica sus pisos en"):
                    fila["notas"] = f"Publica sus pisos en {nombre_portal}: {res['portal']}"
            elif nota.startswith("Publica sus pisos en"):
                fila["notas"] = ""
            fila["url_alquiler"] = res.get("url_alquiler", "")
            fila["vigilar"] = "sí" if res.get("url_alquiler") else ""
            fila["modo"] = res.get("modo", "")
        portales += bool(res and res.get("estado") == "publica_en_portal" and not res.get("revisar"))
        webs += bool(fila.get("web"))
        vig += bool(fila.get("url_alquiler"))
    guardar_csv(campos, filas)
    print(f"Oficinas que publican sus pisos en un portal (Idealista…): {portales}")
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
    # Reintentar las marcas que se dieron por «sin web» sin haber obtenido ningún resultado de búsqueda
    progreso = {k: v for k, v in progreso.items()
                if not (v.get("estado") in ("sin_web", "error") and not v.get("candidatas"))
                and not (v.get("estado") == "web_ok" and "pagina_sin_anuncios" not in v and not v.get("revisado_js"))}
    # Webs que no son de la agencia (el dominio no lleva su nombre): se vuelven a buscar desde cero
    ajenas = [k for k, v in progreso.items() if v.get("web") and not dominio_de_marca(v["web"], v.get("marca", k))]
    for k in ajenas:
        del progreso[k]
    if ajenas:
        print(f"Descarto {len(ajenas)} webs que no llevan el nombre de la agencia: se buscarán de nuevo")
    viejas = [k for k, v in progreso.items() if v.get("web") and (
        v.get("version", 1) < VERSION
        or (v.get("url_alquiler") and not validar_ruta(v["url_alquiler"], v.get("anuncios"))[0]))]
    if viejas:
        print(f"Revisando con los criterios nuevos {len(viejas)} marcas que ya tenían web")
        for k in viejas:
            progreso[k]["revisar"] = True
    if any(v.get("estado") == "busqueda_pendiente" for v in progreso.values()):
        try:
            buscar("inmobiliaria Gràcia Barcelona")
            progreso = {k: v for k, v in progreso.items() if v.get("estado") != "busqueda_pendiente"}
            print("Los buscadores responden: reintento las marcas pendientes de búsqueda")
        except RuntimeError:
            SIN_BUSCADOR["activo"] = True
            print("Buscadores bloqueados hoy: solo pruebo dominios")
    if args.aplicar:
        print("Webs: %d · vigiladas: %d" % aplicar(progreso))
        return 0

    _, filas = cargar_csv()
    marcas: dict[str, dict] = {}
    for f in filas:
        if es_persona(f["nombre"]) and not args.incluir_personas:
            continue
        k = clave_marca(f["nombre"])
        m = marcas.setdefault(k, {"marca": nombre_comercial(f["nombre"]), "municipio": f.get("municipio") or "Barcelona",
                                  "aicat": set(), "tu_zona": False})
        m["aicat"].add(f["aicat"])
        m["tu_zona"] |= r.normalizar(f.get("en_tus_zonas", "")) in r.SI
    pendientes = [(k, m) for k, m in marcas.items() if k not in progreso or progreso[k].get("revisar")]
    pendientes.sort(key=lambda x: (not progreso.get(x[0], {}).get("revisar"), not x[1]["tu_zona"], -len(x[1]["aicat"])))  # primero tus barrios y las cadenas
    print(f"{len(marcas)} marcas · {len(pendientes)} pendientes · esta tanda: {min(args.max, len(pendientes))}")

    fin = time.time() + args.minutos * 60
    hechas = 0
    for k, m in pendientes[: args.max]:
        if time.time() > fin:
            break
        try:
            previo = progreso.get(k, {})
            res = descubrir_marca(m["marca"], m["municipio"], m["aicat"],
                                  previo.get("web") if previo.get("revisar") else None)
        except Exception as e:
            res = {"marca": m["marca"], "estado": "error", "error": str(e)[:150]}
        progreso[k] = res
        hechas += 1
        if res.get("rechazadas") and res["estado"] != "vigilable":
            for u, mot in list(res["rechazadas"].items())[:2]:
                print(f"{'':22}descartada: {u[:80]} ({mot})")
        print(f"[{res['estado']:>18}] {m['marca'][:45]:45} {res.get('url_alquiler') or res.get('web') or ''}"
              + (" (js)" if res.get("modo") == "js" else ""))
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
