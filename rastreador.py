#!/usr/bin/env python3
"""
Rastreador de pisos de alquiler en webs de agencias AICAT de Barcelona.

Lee la lista de agencias (agencias.xlsx o agencias.csv), revisa la página de
alquileres de cada una, detecta anuncios que no había visto antes, aplica tus
filtros (metros, precio, zona) y te avisa por Telegram.

Uso:
  python rastreador.py                 # ciclo normal (lo que ejecuta GitHub cada 5 min)
  python rastreador.py --probar URL    # prueba una web: muestra qué anuncios detecta, sin avisar
  python rastreador.py --probar-telegram
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
import yaml
from bs4 import BeautifulSoup

import contacto

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "estado"
VISTOS = ESTADO / "vistos.json"
TELEGRAM = ESTADO / "telegram.json"
A_CONTACTAR = ESTADO / "a_contactar.json"
FALLOS = ESTADO / "fallos.json"
AVISADOS = ESTADO / "avisados.json"          # anuncios ya enviados a Telegram (para no repetirlos)
BARRIDO = ESTADO / "ultimo_barrido.json"     # resultado del último barrido completo, para afinar criterios

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
SI = {"si", "sí", "s", "yes", "y", "1", "x", "true", "verdadero"}

# Palabras que suelen aparecer en la URL de la ficha de un inmueble
PATRON_FICHA = re.compile(
    r"(inmueble|immoble|ficha|fitxa|propiedad|propietat|property|propert|piso|pis-|/pis/|"
    r"vivienda|habitatge|detalle|detall|anuncio|anunci|referencia|/ref|alquiler|lloguer|"
    r"rent|flat|apartment|apartamento|apartament|inmovilla|/p/|/es/\d|/ca/\d|ficha\.php|id=)",
    re.I,
)
EXCLUIR_URL = re.compile(
    r"(\.pdf|\.jpe?g|\.png|\.webp|mailto:|tel:|whatsapp|facebook|instagram|twitter|linkedin|"
    r"youtube|google\.|/blog|/noticia|/news|aviso-legal|avis-legal|privacidad|privacitat|cookies|"
    r"contacto$|contacte$|/login|/registro|/vender|/valorar|/valoracio|/tasaci|"
    r"wp-login|/author/|/forms?/|contact-individual|/maps\b|imprimir|print|generar-pdf|pdf\.php|"
    r"informacion_gastos|favoritas=|/lang/[a-z]{2}/change|property-submit|que-piso-casa-buscas|/actualitat|/actualidad|_noticia|/20\d\d/\d\d/|[?&]page_id=|"
    r"[?&](vistas|descartadas|sort|orden|ordenada|ascdesc|order|orderby|ob|cart|secc|per_page|es_category%5b0%5d|es_category|es_type)=)",
    re.I,
)
# Inmuebles que no son una vivienda (se mira en el título y la dirección del anuncio, no en la descripción)
RE_NO_VIVIENDA = re.compile(
    r"\b(local(?:es)?|local comercial|inmueble comercial|oficina\w*|despacho\w*|motos?|naves?|nau|industrial|logistic[ao]s?|parkings?|parquing|"
    r"garajes?|aparcament|aparcamiento|plazas? de (garaje|parking|aparcamiento)|placa de parquing|trasteros?|"
    r"terrenos?|solar(es)?|parcelas?|edificios?|lote de|lotes|restaurante|bar restaurante|hotel|hostal|"
    r"habitacion en alquiler|alquiler de habitacion(es)?|habitacio en lloguer|lloguer d.habitacio(ns)?|"
    r"habitacion en piso compartido|piso compartido|pis compartit|room for rent|room in|coliving|traspaso|traspas|temporada|temporal|turistic[oa]?|vacacional|"
    r"holiday|tourism|tourist|short.term|bodega|almacen|magatzem|nave industrial)\b")
PARAMS_BASURA = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                 "fbclid", "gclid", "lang", "idioma"}


# ---------------------------------------------------------------- utilidades
def normalizar(texto: str) -> str:
    texto = texto.replace("’", "'").replace("`", "'").lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto)


def limpiar_url(url: str) -> str:
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in PARAMS_BASURA])
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, "", query, ""))


def mismo_sitio(a: str, b: str) -> bool:
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def cargar_json(ruta: Path, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def guardar_json(ruta: Path, datos) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- extracción de datos
RE_PRECIO = re.compile(
    r"(?:(?:€|eur)\s*(?P<a>\d{1,3}(?:[.,\s]\d{3})+(?!\d)|\d+)(?:[.,]\d{1,2}(?!\d))?)"
    r"|(?:(?P<b>\d{1,3}(?:[.,\s]\d{3})+(?!\d)|\d+)(?:[.,]\d{1,2}(?!\d))?\s*(?:€|eur\b|euros?\b))",
    re.I,
)
RE_MES = re.compile(r"^\s*(?:€)?\s*(?:/|al|per|por|a)?\s*(?:mes|mensual|month|mo\b)", re.I)
RE_M2 = re.compile(r"(\d{2,4})(?:[.,]\d+)?\s*(?:m²|m2|m\^2|m\s2\b|mts\b|metros|metres|sqm)", re.I)
RE_CP = re.compile(r"\b(08\d{3})\b")


def extraer_precio(texto: str) -> int | None:
    candidatos = []
    for m in RE_PRECIO.finditer(texto):
        bruto = m.group("a") or m.group("b")
        valor = int(re.sub(r"[.,\s]", "", bruto))
        if valor < 200:          # cantidades sueltas (gastos, fianzas pequeñas...) no son el alquiler
            continue
        mensual = bool(RE_MES.match(texto[m.end():m.end() + 15]))
        candidatos.append((not mensual, m.start(), valor))
    if not candidatos:
        return None
    candidatos.sort()             # primero los que dicen "/mes", luego por orden de aparición
    return candidatos[0][2]


def extraer_metros(texto: str) -> int | None:
    valores = [int(m.group(1)) for m in RE_M2.finditer(texto)]
    valores = [v for v in valores if 15 <= v < 1000]
    return max(valores) if valores else None


RE_VENTA = re.compile(r"\b(venta|en venda|es ven|se vende|for sale|comprar|compra|compraventa|obra nueva|obra nova|traspaso|traspas)\b")
RE_ALQUILER = re.compile(r"\b(alquiler|alquilar|alquila|lloguer|llogar|es lloga|se alquila|for rent|to rent|arrendamiento)\b")
# Calles con nombre de barrio que en realidad están en otro (Passeig de Gràcia es Eixample)
FRASES_ENGANOSAS = [(r"\b(passeig|paseo|pg\.?|pº) de gracia\b", "eixample"), (r"\bsarria de ter\b", "girona"),
                    (r"\b(el )?besos i (el )?maresme\b", "besos"), (r"\bbesos y (el )?maresme\b", "besos")]


# Carteles de «ya alquilado / reservado» en la tarjeta del listado (texto, clases o imagen del cartel)
RE_NO_DISPONIBLE = re.compile(
    r"\b(llogat|llogada|alquilad[oa]s?|reservad[oa]s?|reservat|reservada|rented|let agreed|leased|"
    r"no disponible|ya no disponible|vendid[oa]|venut|venuda|sold|en tramite|en tramit|senyalitzat|senalizado)\b")
MARCA_NO_DISPONIBLE = re.compile(r"(?<![a-z])(llogat|llogada|alquilad[oa]s?|reservad[oa]s?|reservat|rented|sold|"
                                 r"vendid[oa]s?|venut|venuda|no-?disponible|not-?available|unavailable|let-?agreed)"
                                 r"(?![a-z])", re.I)


def ya_alquilado(texto: str, marcas: str = "") -> str | None:
    """¿La tarjeta dice que el piso ya está alquilado o reservado? Devuelve la palabra que lo indica."""
    m = RE_NO_DISPONIBLE.search(normalizar(texto))
    if m:
        return m.group(1)
    m = MARCA_NO_DISPONIBLE.search(marcas or "")
    return m.group(1).lower() if m else None


VIVIENDA = re.compile(r"\b(piso|pis|atico|atic|casa|duplex|apartamento|apartament|estudio|estudi|loft|vivienda|"
                      r"habitatge|chalet|xalet|torre|masia|flat|apartment|penthouse|house)\b")
EXTRAS = re.compile(r"^(parkings?|parquing|garajes?|aparcament|aparcamiento|plazas? de|placa de|trasteros?|"
                    r"motos?)")


RE_CP_PARENTESIS = re.compile(r"\((\d{5})\)|\b(\d{5})\s+[A-ZÁÉÍÓÚÀÈÒÇ][a-záéíóúàèòç]")
RE_FICHA_CIUDAD = re.compile(r"/ficha/[^/]+/([^/]+)/")  # plataforma Inmovilla: /ficha/<tipo>/<ciudad>/<zona>/…


def otra_ciudad(titulo: str, ruta: str, texto: str, cfg: dict) -> str | None:
    """¿El anuncio es de otra ciudad? Lo dicen el código postal («Pego (03780)»), la dirección web
    («/espana/madrid/…», «/ficha/piso/palamos/…») o el título («…en alquiler en Goya, Madrid»)."""
    for m in RE_CP_PARENTESIS.finditer(f"{titulo} {texto[:300]}"):
        cp = m.group(1) or m.group(2)
        if not cp.startswith("08"):
            return f"CP {cp}"
    propias = [normalizar(w) for z in cfg.get("zonas_municipio", []) for w in cfg["zonas"].get(z, [])]
    m = RE_FICHA_CIUDAD.search(ruta.lower())
    if m and normalizar(m.group(1)) not in ("barcelona", "bcn") and not any(
            w in normalizar(m.group(1)).replace("-", " ") for w in propias):
        return m.group(1)
    ciudades = [normalizar(c) for c in cfg.get("otras_ciudades", [])]
    segmentos = {normalizar(s) for s in re.split(r"[/?&=]", ruta.lower()) if s}
    for c in ciudades:
        if c.replace(" ", "-") in segmentos:
            return c
    t = normalizar(f"{titulo} · {texto[:300]}")
    for c in ciudades:
        if re.search(r"(?<!calle )(?<!carrer )(?<!c/ )(?<!avenida )(?<!avinguda )"
                     r"(\ben|\ba|\bin|,|·|-)\s+" + re.escape(c) + r"\b", t):
            return c
    return None


def no_es_vivienda(texto: str) -> str | None:
    """Devuelve el tipo de inmueble (local, oficina, parking…) si no es una vivienda.

    «Piso de 3 habitaciones» o «Pis amb plaça de parking» son viviendas: el parking o el trastero solo cuentan
    como tipo de inmueble si no se ha nombrado antes una vivienda."""
    t = normalizar(texto).replace("-", " ").replace("_", " ").replace("/", " ")
    if re.match(r"^\s*(habitacion|habitacio|room)\b", t):
        return "habitación"
    for m in RE_NO_VIVIENDA.finditer(t):
        if EXTRAS.match(m.group(1)) and VIVIENDA.search(t[:m.start()]):
            continue
        return m.group(1)
    return None


def es_venta(texto: str) -> bool:
    t = normalizar(texto).replace("-", " ").replace("_", " ")
    return bool(RE_VENTA.search(t)) and not RE_ALQUILER.search(t)


def _buscar(t: str, palabras: list[str]) -> int | None:
    pos = [m.start() for p in palabras for m in [re.search(r"\b" + re.escape(normalizar(p)) + r"\b", t)] if m]
    return min(pos) if pos else None


def detectar_zona(texto: str, cfg: dict, municipios: bool = True) -> tuple[str | None, bool]:
    """Devuelve (zona, descartado).

    zona = una de tus zonas si es lo primero que se nombra; descartado = True si se nombra antes otro barrio
    o municipio (p. ej. «Dreta de l'Eixample»), o una palabra de exclusión."""
    t = normalizar(texto).replace("-", " ").replace("_", " ").replace("´", "'")
    for patron, sustituto in FRASES_ENGANOSAS:
        t = re.sub(patron, sustituto, t)
    for frase in cfg.get("exclusiones", []):
        if re.search(r"\b" + re.escape(normalizar(frase)) + r"\b", t):
            return None, True
    mejor, pos_mejor = None, None
    for zona, palabras in cfg["zonas"].items():
        p = _buscar(t, palabras)
        if p is not None and (pos_mejor is None or p < pos_mejor):
            mejor, pos_mejor = zona, p
    for zona_m in cfg.get("zonas_municipio", []):  # municipio entero (Sant Cugat): basta con que se nombre
        if _buscar(t, cfg["zonas"].get(zona_m, [])) is not None:
            return zona_m, False
    if municipios and _buscar(t, cfg.get("otros_municipios", [])) is not None:  # otra ciudad: fuera, se nombre donde se nombre
        return None, True
    pos_otra = _buscar(t, cfg.get("otras_zonas", []))
    if pos_otra is not None and (pos_mejor is None or pos_otra < pos_mejor):
        return None, True
    if mejor:
        return mejor, False
    cps = set(cfg.get("codigos_postales", []))
    for cp in RE_CP.findall(t):
        if cp in cps:
            return f"CP {cp}", False
    return None, False


TIPOS_ANUNCIO = {"realestatelisting", "offer", "product", "apartment", "house", "singlefamilyresidence", "residence",
                 "accommodation", "place", "breadcrumblist", "webpage", "itempage"}
TIPOS_AGENCIA = {"realestateagent", "organization", "localbusiness", "website", "corporation", "person"}


def _jsonld(soup: BeautifulSoup) -> list[dict]:
    objetos = []
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            datos = json.loads(sc.string or sc.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        pila = datos if isinstance(datos, list) else [datos]
        while pila:
            o = pila.pop()
            if isinstance(o, list):
                pila.extend(o)
            elif isinstance(o, dict):
                if "@graph" in o:
                    pila.extend(o["@graph"] if isinstance(o["@graph"], list) else [o["@graph"]])
                objetos.append(o)
    return objetos


def _textos(o, profundidad=0) -> list[str]:
    """Textos útiles de un objeto JSON-LD (nombre, dirección, url…), sin datos de la agencia que lo publica."""
    if profundidad > 4:
        return []
    if isinstance(o, str):
        return [o]
    if isinstance(o, list):
        return [t for x in o for t in _textos(x, profundidad + 1)]
    if not isinstance(o, dict):
        return []
    tipo = o.get("@type", "")
    tipo = normalizar(" ".join(tipo) if isinstance(tipo, list) else str(tipo))
    if any(t in tipo.split() for t in TIPOS_AGENCIA):
        return []
    out = []
    for k in ("name", "url", "@id", "address", "addressLocality", "addressRegion", "streetAddress", "postalCode",
              "containedInPlace", "itemListElement", "item", "areaServed", "location"):
        if k in o:
            out += _textos(o[k], profundidad + 1)
    return out


def datos_ficha(soup: BeautifulSoup) -> dict:
    """Lo que la ficha dice de sí misma: dónde está, título, precio y metros.

    Se fía primero de los datos estructurados (JSON-LD de schema.org, dirección canónica), que muchas plataformas de
    agencias rellenan con ciudad y barrio exactos, y nunca de la dirección de la oficina de la agencia."""
    lugar, precio, metros, nombre = [], None, None, ""
    for o in _jsonld(soup):
        tipo = o.get("@type", "")
        tipo = normalizar(" ".join(tipo) if isinstance(tipo, list) else str(tipo))
        if any(t in tipo.split() for t in TIPOS_AGENCIA) or not any(t in tipo.split() for t in TIPOS_ANUNCIO):
            continue
        lugar += _textos(o)
        nombre = nombre or (o.get("name") if isinstance(o.get("name"), str) and "breadcrumb" not in tipo else "")
        ofertas = o.get("offers") or (o if "offer" in tipo.split() else None)
        for of in (ofertas if isinstance(ofertas, list) else [ofertas] if ofertas else []):
            if isinstance(of, dict):
                try:
                    precio = precio or int(float(str(of.get("price", "")).replace(",", ".")))
                except ValueError:
                    pass
        tam = o.get("floorSize")
        if isinstance(tam, dict):
            try:
                metros = metros or int(float(str(tam.get("value", ""))))
            except ValueError:
                pass
    for sel in ["link[rel=canonical]", "meta[property='og:url']"]:
        el = soup.select_one(sel)
        if el and (el.get("href") or el.get("content")):
            lugar.append(urlparse(el.get("href") or el.get("content")).path)
    titulo = ""
    for sel in ["meta[property='og:title']", "h1"]:
        el = soup.select_one(sel)
        t = (el.get("content") if el and el.name == "meta" else el.get_text(" ", strip=True) if el else "") or ""
        if len(t.strip()) >= 6:
            titulo = titulo or t.strip()
            lugar.append(t)
    for sel in ["[class*=breadcrumb]", "[class*=location]", "[class*=localizacion]", "[class*=ubicacion]",
                "[class*=address]", "[class*=direccion]", "[class*=zona]", "[class*=barrio]", "[itemprop=address]"]:
        for el in soup.select(sel)[:2]:
            if not el.find_parent(["footer", "header", "nav"]):
                lugar.append(el.get_text(" ", strip=True)[:200])
    return {"lugar": " · ".join(x for x in lugar if x), "titulo": (nombre or titulo)[:140],
            "precio": precio if precio and 200 <= precio < 10_000_000 else None,
            "metros": metros if metros and 15 <= metros < 1000 else None}


def cabecera_ficha(soup: BeautifulSoup) -> str:
    """Título, migas de pan y descripción corta de una ficha: donde se dice de verdad qué y dónde es."""
    partes = [soup.title.get_text(" ", strip=True) if soup.title else ""]
    for sel in ["meta[property='og:title']", "meta[name='description']", "meta[property='og:description']"]:
        m = soup.select_one(sel)
        if m and m.get("content"):
            partes.append(m["content"])
    for sel in ["h1", "[class*=breadcrumb]", "[class*=location]", "[class*=ubicacion]", "[class*=address]",
                "[class*=zona]", "[itemprop=address]"]:
        for el in soup.select(sel)[:2]:
            partes.append(el.get_text(" ", strip=True)[:300])
    return " · ".join(p for p in partes if p)


def texto_principal(soup: BeautifulSoup) -> str:
    """Texto de la página sin cabecera, menú ni pie (donde suele estar la dirección de la agencia)."""
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form", "svg"]):
        tag.decompose()
    for sel in ["[class*=footer]", "[id*=footer]", "[class*=menu]", "[id*=menu]", "[class*=cookie]",
                "[class*=related]", "[class*=similar]", "[class*=relacionad]"]:
        for tag in soup.select(sel):
            tag.decompose()
    cuerpo = soup.find("main") or soup.find("article") or soup.body or soup
    return re.sub(r"\s+", " ", cuerpo.get_text(" ", strip=True))


def extraer_anuncios(html_txt: str, url_base: str) -> list[dict]:
    """Encuentra los enlaces que parecen fichas de inmuebles y el texto de su tarjeta."""
    soup = BeautifulSoup(html_txt, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    base_limpia = limpiar_url(url_base)
    enlaces: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            continue
        url = urljoin(url_base, href)
        if not url.startswith("http") or EXCLUIR_URL.search(url) or not mismo_sitio(url, url_base):
            continue
        limpia = limpiar_url(url)
        if limpia == base_limpia:
            continue
        # Mismo listado con otros filtros (zona, orden, tipo…): no es un anuncio. Ej. Coldwell «search?z=9»
        if urlparse(url).path.rstrip("/") == urlparse(url_base).path.rstrip("/") and urlparse(url).netloc.removeprefix("www.") == urlparse(url_base).netloc.removeprefix("www."):
            continue
        ruta = urlparse(url).path + "?" + urlparse(url).query
        if not PATRON_FICHA.search(ruta) or not re.search(r"\d", ruta):
            continue
        # Paginación del propio listado: no es un anuncio
        if (re.search(r"[?&](page|pag|pagina|p|paged)=\d+$", "?" + urlparse(url).query, re.I)
                or re.search(r"/(page|pagina|pag)/\d+/?$", urlparse(url).path, re.I)):
            continue
        # Listados por zona («…/en-alquiler-barcelona/galvany/1»): acaban en un número de página
        if re.search(r"/\d{1,2}/?$", urlparse(url).path) and not re.search(r"\d{3,}", urlparse(url).path):
            continue
        # Enlaces a otros listados («19 inmuebles en alquiler en León»), no a un piso
        if re.match(r"^\s*\d+\s+(inmuebles?|propiedades|pisos|anuncios|viviendas|immobles|habitatges)\b",
                    a.get_text(" ", strip=True), re.I):
            continue
        enlaces.setdefault(limpia, []).append(a)

    anuncios = []
    for url, anchors in enlaces.items():
        mejor, marcas = "", ""
        for a in anchors:
            nodo, texto = a, a.get_text(" ", strip=True)
            for _ in range(8):  # subir hasta la "tarjeta" que contiene solo este anuncio
                padre = nodo.parent
                if padre is None or padre.name in ("body", "html", "[document]"):
                    break
                otros = {limpiar_url(urljoin(url_base, x["href"])) for x in padre.find_all("a", href=True)}
                otros = {o for o in otros if o in enlaces}
                if len(otros) > 1:
                    break
                t = padre.get_text(" ", strip=True)
                if len(t) > 1500:
                    break
                nodo, texto = padre, t
            if len(texto) > len(mejor):
                mejor = texto
                # clases e imágenes de la tarjeta: algunas webs marcan «alquilado» solo con un cartel
                partes = [" ".join(nodo.get("class", []))] if hasattr(nodo, "get") else []
                for el in (nodo.find_all(True) if hasattr(nodo, "find_all") else [])[:80]:
                    partes.append(" ".join(el.get("class", [])))
                    if el.name == "img":
                        partes += [el.get("src", ""), el.get("alt", "")]
                marcas = " ".join(p for p in partes if p)[:2000]
        titulo = next((a.get("title") or a.get_text(" ", strip=True) for a in anchors
                       if (a.get("title") or a.get_text(strip=True))), "")
        anuncios.append({"url": url, "texto": re.sub(r"\s+", " ", mejor)[:1500], "titulo": titulo[:140],
                         "marcas": marcas})
    return anuncios


RE_PAGINA = re.compile(r"([?&](page|pag|pagina|p|pg|numpag|start|offset)=(\d+))|(/(page|pagina|pag|p)/(\d+)/?$)|"
                       r"(/(\d{1,2})/?$)", re.I)


def otras_paginas(html_txt: str, url_base: str, maximo: int) -> list[str]:
    """Páginas 2, 3… del mismo listado (para el barrido completo)."""
    if maximo <= 1:
        return []
    soup = BeautifulSoup(html_txt, "html.parser")
    base = urlparse(url_base)
    paginas: dict[int, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(url_base, a["href"].strip()).split("#")[0]
        p = urlparse(url)
        if not mismo_sitio(url, url_base) or limpiar_url(url) == limpiar_url(url_base):
            continue
        texto = a.get_text(" ", strip=True).lower()
        m = RE_PAGINA.search(p.path + ("?" + p.query if p.query else ""))
        if not m:
            continue
        num = int(next(g for g in (m.group(3), m.group(6), m.group(8)) if g))
        mismo_listado = p.path.rstrip("/").startswith(base.path.rstrip("/")) or p.path.split("/")[:3] == base.path.split("/")[:3]
        if not mismo_listado or not (2 <= num <= 200):
            continue
        if m.group(8) and not (texto.isdigit() or a.get("rel") == ["next"] or re.search(r"(siguiente|seguent|next|›|»)", texto)):
            continue  # «/2» al final de la ruta solo cuenta si el enlace es de paginación
        if m.group(2) and m.group(2).lower() in ("start", "offset"):
            num = len(paginas) + 2
        paginas.setdefault(num, url)
    return [paginas[n] for n in sorted(paginas)][: maximo - 1]


# ---------------------------------------------------------------- red
SESION = requests.Session()
SESION.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,ca;q=0.8"})


def descargar(url: str, timeout: int, modo: str = "") -> str:
    if modo.strip().lower() == "js":
        return descargar_js(url, timeout)
    r = SESION.get(url, timeout=timeout)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def descargar_js(url: str, timeout: int) -> str:
    """Para webs que cargan los pisos con JavaScript (columna modo = js)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        nav = p.chromium.launch(channel="chrome")  # Chrome viene instalado en GitHub Actions
        pag = nav.new_page(user_agent=UA, locale="es-ES")
        pag.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        contenido = pag.content()
        nav.close()
        return contenido


def enviar_telegram(texto: str) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    # Uno o varios chats separados por comas: el tuyo, el de tu pareja o el de un grupo (empieza por -)
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        print("[telegram desactivado] " + texto.replace("\n", " | "))
        return
    for chat in chats:
        _enviar_a(token, chat, texto)


def _enviar_a(token: str, chat: str, texto: str) -> None:
    for intento in range(3):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
            "chat_id": chat, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": "false"})
        if r.status_code == 429:
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)))
            continue
        if not r.ok:
            print(f"Error Telegram {r.status_code}: {r.text[:200]}", file=sys.stderr)
        break
    time.sleep(1.1)


# ---------------------------------------------------------------- agencias
def _leer_tabla(ruta: Path) -> list[dict]:
    if not ruta.exists():
        return []
    if ruta.suffix == ".xlsx":
        from openpyxl import load_workbook
        ws = load_workbook(ruta, read_only=True, data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        cab = [normalizar(str(c or "")).strip().replace(" ", "_") for c in next(it)]
        return [{cab[i]: ("" if v is None else str(v)).strip() for i, v in enumerate(f) if i < len(cab)} for f in it]
    with ruta.open(encoding="utf-8-sig", newline="") as f:
        return [{normalizar(k).strip().replace(" ", "_"): (v or "").strip() for k, v in fila.items() if k}
                for fila in csv.DictReader(f)]


# Portales que bloquean a los bots: el rastreador no puede leerlos. Las agencias que publican ahí se cubren con
# una búsqueda guardada con alertas en el propio portal (ver README).
PORTALES = re.compile(r"(idealista\.com|fotocasa\.es|habitaclia\.com|pisos\.com|yaencontre\.com|milanuncios\.com)", re.I)


def portal_de(url: str) -> str | None:
    m = PORTALES.search(urlparse(url).netloc)
    return {"idealista": "Idealista", "fotocasa": "Fotocasa", "habitaclia": "Habitaclia", "pisos": "pisos.com",
            "yaencontre": "yaencontre", "milanuncios": "Milanuncios"}[m.group(1).split(".")[0].lower()] if m else None


def cargar_agencias() -> list[dict]:
    """agencias.csv (lista completa + webs descubiertas) con tus cambios de mis_agencias.csv encima.

    mis_agencias.csv es lo que exporta el panel: solo las agencias que has tocado. Un valor
    vacío no cambia nada; vigilar = no desactiva una agencia que el descubridor había activado."""
    base = {f.get("aicat") or f.get("url_alquiler"): f for f in (_leer_tabla(RAIZ / "agencias.xlsx")
                                                                   or _leer_tabla(RAIZ / "agencias.csv"))}
    for cambio in _leer_tabla(RAIZ / "mis_agencias.csv"):
        fila = base.setdefault(cambio.get("aicat") or cambio.get("url_alquiler"), {})
        fila.update({k: v for k, v in cambio.items() if v})

    por_url: dict[tuple, dict] = {}
    for fila in base.values():
        urls = tuple(u for u in re.split(r"[|\n ]+", fila.get("url_alquiler", ""))
                     if u.startswith("http") and not portal_de(u))  # los portales bloquean al bot: no se piden
        if not urls or normalizar(fila.get("vigilar", "")) not in SI:
            continue
        nombre = fila.get("nombre", "") or urls[0]
        m = re.search(r"\(([^()]+)\)\s*$", nombre)
        ag = por_url.setdefault(urls, {
            "id": fila.get("aicat") or urls[0], "nombre": m.group(1) if m else nombre, "urls": list(urls),
            "modo": fila.get("modo", ""), "contactado": False, "en_zona": False, "oficinas": 0,
            "direccion": ", ".join(x for x in [fila.get("direccion", ""), fila.get("cp", ""), fila.get("municipio", "")] if x),
            "web": fila.get("web", "")})
        ag["oficinas"] += 1
        ag["contactado"] |= normalizar(fila.get("contactado", "")) in SI
        # Si la oficina está en tus distritos, un anuncio sin barrio probablemente es de la zona
        ag["en_zona"] |= normalizar(fila.get("en_tus_zonas", "")) in SI
    agencias = list(por_url.values())
    for ag in agencias:
        if ag["oficinas"] > 1:
            ag["nombre"] += f" ({ag['oficinas']} oficinas)"
    return agencias


# ---------------------------------------------------------------- lógica principal
def evaluar(anuncio: dict, cfg: dict, timeout: int, modo: str, abrir: bool, agencia_en_zona: bool = True) -> dict:
    """Decide si un anuncio nuevo merece aviso. Orden: ¿es un anuncio de vivienda en alquiler? → ¿dónde está?
    (tarjeta y dirección web; si no lo dicen, la ficha) → precio y metros."""
    f = cfg["filtros"]
    texto = anuncio["texto"]
    ruta = urlparse(anuncio["url"]).path + " " + urlparse(anuncio["url"]).query
    identidad = f"{anuncio['titulo']} {ruta} {texto[:300]}"
    precio, metros = extraer_precio(texto), extraer_metros(texto)
    venta = es_venta(identidad)
    alquilado = ya_alquilado(f"{anuncio['titulo']} {texto[:400]}", anuncio.get("marcas", ""))
    tipo = no_es_vivienda(f"{anuncio['titulo']} {ruta}") if f.get("solo_viviendas", True) else None
    # La zona se decide por el título, la dirección web y la tarjeta; nunca por menús o pisos relacionados
    zona, excluido = detectar_zona(f"{anuncio['titulo']} · {ruta} · {texto}", cfg)
    ciudad = None if zona else otra_ciudad(anuncio["titulo"], urlparse(anuncio["url"]).path, texto, cfg)
    excluido = excluido or bool(ciudad)
    precios_altos = [p for p in [precio] if p and p >= 30000]
    if abrir and not venta and not alquilado and not tipo and not excluido and (precio is None or metros is None or zona is None):
        try:
            anuncio["_html"] = descargar(anuncio["url"], timeout, modo)
            soup = BeautifulSoup(anuncio["_html"], "html.parser")
            ficha = datos_ficha(soup)
            cab = cabecera_ficha(soup)
            cuerpo = texto_principal(BeautifulSoup(anuncio["_html"], "html.parser"))[:4000]
            venta = venta or es_venta(f"{ficha['titulo']} {cab}")
            if f.get("solo_viviendas", True):
                tipo = no_es_vivienda(ficha["titulo"])
            # Un importe de venta en la ficha (≥ 30.000 €) delata una venta aunque haya una «cuota» de 1.000 €/mes
            todos = [int(re.sub(r"[.,\s]", "", m.group("a") or m.group("b"))) for m in RE_PRECIO.finditer(cab + " " + cuerpo)]
            precios_altos += [p for p in todos + [ficha["precio"] or 0] if p >= 30000]
            precio = precio or ficha["precio"] or extraer_precio(cab) or extraer_precio(cuerpo)
            metros = metros or ficha["metros"] or extraer_metros(cab) or extraer_metros(cuerpo)
            # Dónde está: datos estructurados y cabecera de la ficha; el cuerpo solo como último recurso
            for fuente, municipios in ((ficha["lugar"], True), (cab, True), (cuerpo[:1500], False)):
                if zona is not None or excluido:
                    break
                zona, excluido = detectar_zona(fuente, cfg, municipios)
            if zona is None and not excluido:
                ciudad = otra_ciudad(ficha["titulo"], ficha["lugar"], cab, cfg)
                excluido = bool(ciudad)
            alquilado = alquilado or ya_alquilado(ficha["titulo"])
            if ficha["titulo"]:
                anuncio["titulo"] = ficha["titulo"]
            elif not anuncio["titulo"] or len(anuncio["titulo"]) < 8:
                anuncio["titulo"] = (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else cuerpo)[:120]
        except Exception as e:  # la ficha falla: seguimos con lo que hay
            anuncio["error_ficha"] = str(e)[:100]
    motivo = None
    if venta:
        motivo = "es una venta"
    elif alquilado:
        motivo = f"ya alquilado o reservado ({alquilado})"
    elif tipo:
        motivo = f"no es una vivienda ({tipo})"
    elif precios_altos:
        motivo = f"parece venta ({max(precios_altos)} €)"
    elif re.match(r"^\s*\d+\s+(inmuebles?|propiedades|pisos|anuncios|viviendas|immobles)\b", normalizar(anuncio["titulo"])):
        motivo = "no es un anuncio (es un listado)"
    elif excluido:
        motivo = "fuera de tus zonas" + (" (otra ciudad)" if ciudad else "")
    elif precio is None and metros is None and zona is None:
        motivo = "no parece un anuncio (sin precio, metros ni barrio)"
    elif zona is None and f.get("zona_obligatoria", True):
        motivo = "no dice en qué barrio está"
    elif precio is None and f.get("precio_obligatorio", True):
        motivo = "no indica el precio"
    elif precio is not None and precio > 10000:
        motivo = f"parece venta ({precio} €)"
    elif precio is not None and precio > f["precio_maximo"]:
        motivo = f"precio {precio} € > {f['precio_maximo']}"
    elif metros is not None and metros < f.get("metros_minimos_con_aviso", f["metros_minimos"]):
        motivo = f"{metros} m² < {f.get('metros_minimos_con_aviso', f['metros_minimos'])}"
    elif metros is None and not f.get("avisar_si_faltan_metros", True):
        motivo = "no indica los metros"
    return {**anuncio, "precio": precio, "metros": metros, "zona": zona, "motivo_descarte": motivo}


def mensaje(agencia: dict, a: dict) -> str:
    e = html.escape
    precio = f"{a['precio']} €/mes" if a["precio"] else "⚠️ precio no indicado"
    metros = f"{a['metros']} m²" if a["metros"] else "⚠️ m² no indicados"
    minimo = (CFG_ACTUAL.get("filtros") or {}).get("metros_minimos")
    if a["metros"] and minimo and a["metros"] < minimo:
        metros += f" ⚠️ (menos de {minimo})"
    zona = a["zona"] or "⚠️ zona no indicada"
    contacto = "\n✅ Ya tienen tu perfil" if agencia.get("contactado") else ""
    cabecera = "Piso disponible" if a.get("existente") else "Nuevo piso"
    return (f"🏠 <b>{cabecera} · {e(agencia['nombre'])}</b>\n"
            f"💶 {e(precio)} · 📐 {e(metros)}\n📍 {e(zona)}\n"
            f"{e(a['titulo'][:120])}\n{e(a['url'])}{contacto}")


def revisar_agencia(agencia: dict, cfg: dict, vistos: dict, avisados: dict | None = None,
                    barrido: bool = False, reenviar: bool = False) -> tuple[list, list, str | None, list]:
    """Devuelve (anuncios a avisar, todas las urls vistas, error, evaluaciones).

    Normal: solo se evalúan los anuncios nuevos. Barrido (o primera vez que se ve la agencia): se evalúan todos los
    publicados, siguiendo las páginas 2, 3… del listado, y se avisa de los que cumplan y no se hayan enviado ya."""
    r = cfg["rastreo"]
    avisados = avisados if avisados is not None else {}
    todas, candidatos = [], []
    previos = set(vistos.get(agencia["id"], {}).get("urls", []))
    primera_vez = agencia["id"] not in vistos
    completo = barrido or (primera_vez and r.get("avisar_existentes_al_empezar", True))
    try:
        for url in agencia["urls"]:
            html_listado = descargar(url, r["timeout_segundos"], agencia["modo"])
            paginas = [(url, html_listado)]
            if completo:
                for extra in otras_paginas(html_listado, url, r.get("max_paginas_barrido", 10)):
                    try:
                        paginas.append((extra, descargar(extra, r["timeout_segundos"], agencia["modo"])))
                    except Exception:
                        break
            for u, h in paginas:
                anuncios = [a for a in extraer_anuncios(h, u) if a["url"] not in todas]
                todas += [a["url"] for a in anuncios]
                if completo:
                    candidatos += [a for a in anuncios if reenviar or a["url"] not in avisados]
                else:
                    candidatos += [a for a in anuncios if a["url"] not in previos and a["url"] not in avisados]
    except Exception as e:
        return [], [], f"{type(e).__name__}: {str(e)[:150]}", []
    if primera_vez and not completo:  # primera pasada sin barrido: guardamos lo que ya había sin avisar
        return [], todas, None, []
    limite = r.get("max_fichas_barrido", 150) if completo else r.get("max_fichas_por_agencia", 15)
    avisos, evaluaciones = [], []
    for i, a in enumerate(candidatos):
        ev = evaluar(a, cfg, r["timeout_segundos"], agencia["modo"], r.get("abrir_fichas", True) and i < limite)
        evaluaciones.append({k: ev.get(k) for k in ("url", "titulo", "precio", "metros", "zona", "motivo_descarte")})
        if ev["motivo_descarte"]:
            if not completo:
                print(f"  - descartado ({ev['motivo_descarte']}): {a['url']}")
        else:
            try:  # fotos, teléfono y formulario de la ficha
                html_ficha = ev.get("_html") or descargar(a["url"], r["timeout_segundos"], agencia["modo"])
                ev["detalle"] = contacto.extraer_detalle(html_ficha, a["url"])
            except Exception:
                ev["detalle"] = {}
            ev["existente"] = completo
            avisos.append(ev)
        ev.pop("_html", None)
    return avisos, todas, None, evaluaciones


def cargar_tg() -> dict:
    tg = cargar_json(TELEGRAM, {"offset": 0, "pendientes": {}})
    limite = time.time() - 30 * 86400  # los botones de anuncios de hace más de un mes caducan
    tg["pendientes"] = {k: v for k, v in tg.get("pendientes", {}).items() if v.get("fecha", 0) > limite}
    return tg


def guardar_tg(tg: dict) -> None:
    if cargar_json(TELEGRAM, None) != tg:
        guardar_json(TELEGRAM, tg)


CFG_ACTUAL: dict = {}


def ciclo(cfg: dict, barrido: bool = False, reenviar: bool = False) -> int:
    CFG_ACTUAL.clear()
    CFG_ACTUAL.update(cfg)
    tg_estado = cargar_tg()
    avisados = cargar_json(AVISADOS, {})
    resumen: dict[str, int] = {}
    detalle_barrido: list[dict] = []
    agencias = cargar_agencias()
    vistos, fallos = cargar_json(VISTOS, {}), cargar_json(FALLOS, {})
    a_contactar = cargar_json(A_CONTACTAR, {})
    dias = cfg["rastreo"].get("dias_sin_novedades", 180)
    ahora = time.time()
    activas = []
    for ag in agencias:
        if ag["id"] in a_contactar:
            continue
        reg = vistos.get(ag["id"])
        if reg and dias and ahora - max(reg.get("desde", ahora), reg.get("ultimo_nuevo", 0)) > dias * 86400:
            a_contactar[ag["id"]] = {"nombre": ag["nombre"], "direccion": ag.get("direccion", ""),
                                     "web": ag.get("web", ""), "url_alquiler": ag["urls"][0],
                                     "desde": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
            enviar_telegram(f"🏢 <b>{html.escape(ag['nombre'])}</b> lleva {dias // 30} meses sin publicar ningún piso "
                            f"nuevo en su web. Dejo de vigilarla: mejor pasar por la oficina y dejar vuestro perfil.\n"
                            f"📍 {html.escape(ag.get('direccion', '') or 'dirección no disponible')}")
            continue
        activas.append(ag)
    if not barrido:  # las agencias nuevas se revisan a fondo: unas pocas por pasada para no pasarse de tiempo
        tope = cfg["rastreo"].get("agencias_nuevas_por_ciclo", 8)
        nuevas = [ag for ag in activas if ag["id"] not in vistos]
        if cfg["rastreo"].get("avisar_existentes_al_empezar", True) and len(nuevas) > tope:
            esperan = {ag["id"] for ag in nuevas[tope:]}
            print(f"{len(esperan)} agencias nuevas esperan a la siguiente pasada")
            activas = [ag for ag in activas if ag["id"] not in esperan]
    if len(activas) != len(agencias):
        guardar_json(A_CONTACTAR, a_contactar)
    agencias = activas
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · {len(agencias)} agencias vigiladas"
          f" · {len(a_contactar)} para contactar en persona")
    total = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futuros = {ex.submit(revisar_agencia, ag, cfg, vistos, avisados, barrido, reenviar): ag for ag in agencias}
        for fut in as_completed(futuros):
            ag = futuros[fut]
            avisos, urls, error, evaluaciones = fut.result()
            for ev in evaluaciones:
                motivo = re.sub(r"\d[\d.]*", "N", ev["motivo_descarte"] or "ENVIADO")
                resumen[motivo] = resumen.get(motivo, 0) + 1
                detalle_barrido.append({**ev, "agencia": ag["nombre"]})
            if error:
                n = fallos.get(ag["id"], 0) + 1
                fallos[ag["id"]] = n
                print(f"✗ {ag['nombre']}: {error} (fallo {n})")
                if n == cfg["rastreo"].get("avisar_tras_fallos", 6):
                    enviar_telegram(f"⚠️ La web de <b>{html.escape(ag['nombre'])}</b> falla desde hace "
                                    f"{n} revisiones: {html.escape(error)}\nRevisa la URL en la hoja.")
                continue
            fallos.pop(ag["id"], None)
            if not urls:
                print(f"? {ag['nombre']}: no se detecta ningún anuncio; prueba --probar o modo js")
            nueva = ag["id"] not in vistos
            reg = vistos.setdefault(ag["id"], {"urls": []})
            reg.setdefault("desde", time.time())
            if not nueva and any(u not in set(reg["urls"]) for u in urls):
                reg["ultimo_nuevo"] = time.time()  # ha publicado algo (cumpla o no vuestros filtros)
            reg["urls"] = list(dict.fromkeys(urls + reg["urls"]))[:3000]
            for a in avisos:
                if a["url"] in avisados and not reenviar:
                    continue  # otra oficina de la misma red ya lo envió
                contacto.notificar(mensaje(ag, a), a["url"], a.get("detalle") or {}, tg_estado["pendientes"],
                                   {"titulo": a["titulo"][:120], "agencia": ag["nombre"]})
                avisados[a["url"]] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                total += 1
                if total % 10 == 0:  # si el barrido se cortara, no se repiten los ya enviados
                    guardar_json(AVISADOS, avisados)
                    guardar_tg(tg_estado)
                if barrido or len(avisos) > 3:
                    time.sleep(2)  # Telegram limita los mensajes por minuto en los grupos
            print(f"✓ {ag['nombre']}: {len(urls)} anuncios, {len(avisos)} avisos")
    guardar_json(VISTOS, vistos)
    guardar_json(FALLOS, fallos)
    guardar_json(AVISADOS, avisados)
    guardar_tg(tg_estado)
    print(f"Avisos enviados: {total}")
    if barrido:
        guardar_json(BARRIDO, {"fecha": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "resumen": resumen,
                               "anuncios": detalle_barrido})
        revisados = sum(resumen.values())
        lineas = "\n".join(f"· {m}: {n}" for m, n in sorted(resumen.items(), key=lambda x: -x[1]) if m != "ENVIADO")
        enviar_telegram(f"🔎 <b>Barrido completo terminado</b>\n{len(agencias)} agencias · {revisados} anuncios revisados"
                        f" · <b>{total} os encajan</b> (enviados arriba)\n\nDescartados:\n{html.escape(lineas[:3000])}")
    return 0


def probar(url: str, cfg: dict, modo: str) -> None:
    anuncios = extraer_anuncios(descargar(url, 30, modo), url)
    print(f"{len(anuncios)} anuncios detectados en {url}\n")
    for a in anuncios[:40]:
        ev = evaluar(a, cfg, 30, modo, cfg["rastreo"].get("abrir_fichas", True))
        estado = "AVISARÍA" if not ev["motivo_descarte"] else f"descarta: {ev['motivo_descarte']}"
        print(f"[{estado}] {ev['precio']} € · {ev['metros']} m² · {ev['zona']}\n   {a['url']}")
        if not ev["motivo_descarte"]:
            try:
                d = contacto.extraer_detalle(ev.get("_html") or descargar(a["url"], 30, modo), a["url"])
                print(f"   fotos: {len(d['fotos'])} · teléfono: {', '.join(d['telefonos']) or '—'} · "
                      f"formulario: {'sí' if d['formulario'] else 'no'}{' (con captcha)' if d['captcha'] else ''}")
            except Exception as e:
                print(f"   (no se pudo abrir la ficha: {e})")
    if not anuncios:
        print("No se ha detectado ningún anuncio. Si la página los carga con JavaScript, "
              "pon 'js' en la columna modo y vuelve a probar con --modo js.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probar", metavar="URL")
    ap.add_argument("--modo", default="")
    ap.add_argument("--probar-telegram", action="store_true")
    ap.add_argument("--sin-escuchar", action="store_true", help="no esperar pulsaciones de botones")
    ap.add_argument("--probar-formulario", metavar="URL", help="rellena el formulario de un anuncio SIN enviarlo")
    ap.add_argument("--barrido", action="store_true",
                    help="revisa TODOS los anuncios publicados (no solo los nuevos) y envía los que encajen")
    ap.add_argument("--reenviar", action="store_true", help="con --barrido: envía también los que ya se enviaron")
    args = ap.parse_args()
    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8"))
    if args.probar_formulario:
        ok, txt = contacto.enviar_formulario(args.probar_formulario, "prueba", enviar=False)
        print(("OK: " if ok else "NO: ") + txt)
        return 0
    if args.probar_telegram:
        enviar_telegram("✅ El rastreador de pisos está conectado a Telegram.")
        return 0
    if args.probar:
        probar(args.probar, cfg, args.modo)
        return 0
    inicio = time.time()
    codigo = ciclo(cfg, barrido=args.barrido, reenviar=args.reenviar)
    escuchar = cfg["rastreo"].get("escuchar_botones_segundos", 240)
    if escuchar and not args.sin_escuchar:
        # Atiende los botones «Enviar mis datos» hasta que empiece la siguiente revisión
        tg_estado = cargar_tg()
        try:
            contacto.escuchar(tg_estado, inicio + escuchar)
        finally:
            guardar_tg(tg_estado)
    return codigo


if __name__ == "__main__":
    sys.exit(main())
