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
FALLOS = ESTADO / "fallos.json"

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
    r"contacto$|contacte$|/login|/registro|/vender|/valorar|/valoracio|/tasaci)",
    re.I,
)
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
    r"(?:€\s*(?P<a>\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?)"
    r"|(?:(?P<b>\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?\s*(?:€|eur\b|euros?\b))",
    re.I,
)
RE_MES = re.compile(r"^\s*(?:€)?\s*(?:/|al|per|por|a)?\s*(?:mes|mensual|month|mo\b)", re.I)
RE_M2 = re.compile(r"(\d{2,4})(?:[.,]\d+)?\s*(?:m²|m2|m\^2|m\s2\b|mts\b|metros|metres|sqm)", re.I)
RE_CP = re.compile(r"\b(08\d{3})\b")


def extraer_precio(texto: str) -> int | None:
    candidatos = []
    for m in RE_PRECIO.finditer(texto):
        bruto = m.group("a") or m.group("b")
        valor = int(re.sub(r"[.\s]", "", bruto))
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


def detectar_zona(texto: str, cfg: dict) -> tuple[str | None, bool]:
    """Devuelve (zona encontrada, excluido)."""
    t = normalizar(texto)
    for frase in cfg.get("exclusiones", []):
        if re.search(r"\b" + re.escape(normalizar(frase)) + r"\b", t):
            return None, True
    for zona, palabras in cfg["zonas"].items():
        for p in palabras:
            if re.search(r"\b" + re.escape(normalizar(p)) + r"\b", t):
                return zona, False
    cps = set(cfg.get("codigos_postales", []))
    for cp in RE_CP.findall(t):
        if cp in cps:
            return f"CP {cp}", False
    return None, False


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
        ruta = urlparse(url).path + "?" + urlparse(url).query
        if not PATRON_FICHA.search(ruta) or not re.search(r"\d", ruta):
            continue
        # Paginación del propio listado: no es un anuncio
        if re.search(r"([?&](page|pag|pagina|p)=\d+$)|(/(page|pagina)/\d+/?$)", ruta, re.I):
            continue
        enlaces.setdefault(limpia, []).append(a)

    anuncios = []
    for url, anchors in enlaces.items():
        mejor = ""
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
        titulo = next((a.get("title") or a.get_text(" ", strip=True) for a in anchors
                       if (a.get("title") or a.get_text(strip=True))), "")
        anuncios.append({"url": url, "texto": re.sub(r"\s+", " ", mejor)[:1500], "titulo": titulo[:140]})
    return anuncios


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
        urls = tuple(u for u in re.split(r"[|\n ]+", fila.get("url_alquiler", "")) if u.startswith("http"))
        if not urls or normalizar(fila.get("vigilar", "")) not in SI:
            continue
        nombre = fila.get("nombre", "") or urls[0]
        m = re.search(r"\(([^()]+)\)\s*$", nombre)
        ag = por_url.setdefault(urls, {
            "id": fila.get("aicat") or urls[0], "nombre": m.group(1) if m else nombre, "urls": list(urls),
            "modo": fila.get("modo", ""), "contactado": False, "en_zona": False, "oficinas": 0})
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
    texto = anuncio["texto"]
    precio, metros = extraer_precio(texto), extraer_metros(texto)
    zona, excluido = detectar_zona(texto, cfg)
    if abrir and (precio is None or metros is None or zona is None):
        try:
            anuncio["_html"] = descargar(anuncio["url"], timeout, modo)
            ficha = texto_principal(BeautifulSoup(anuncio["_html"], "html.parser"))
            precio = precio or extraer_precio(ficha)
            metros = metros or extraer_metros(ficha)
            if zona is None and not excluido:
                zona, excluido = detectar_zona(ficha[:6000], cfg)
            if not anuncio["titulo"] or len(anuncio["titulo"]) < 8:
                anuncio["titulo"] = ficha[:120]
        except Exception as e:  # la ficha falla: seguimos con lo que hay
            anuncio["error_ficha"] = str(e)[:100]
    f = cfg["filtros"]
    motivo = None
    if excluido:
        motivo = "excluido por palabra clave"
    elif precio is not None and precio > 10000:
        motivo = f"parece venta ({precio} €)"
    elif precio is not None and precio > f["precio_maximo"]:
        motivo = f"precio {precio} € > {f['precio_maximo']}"
    elif metros is not None and metros < f["metros_minimos"]:
        motivo = f"{metros} m² < {f['metros_minimos']}"
    elif (precio is None or metros is None) and not f.get("avisar_si_faltan_datos", True):
        motivo = "faltan precio o metros"
    elif zona is None and not (f.get("avisar_si_zona_desconocida", True) and agencia_en_zona):
        motivo = "zona no indicada" + ("" if agencia_en_zona else " (agencia fuera de tus distritos)")
    return {**anuncio, "precio": precio, "metros": metros, "zona": zona, "motivo_descarte": motivo}


def mensaje(agencia: dict, a: dict) -> str:
    e = html.escape
    precio = f"{a['precio']} €/mes" if a["precio"] else "⚠️ precio no indicado"
    metros = f"{a['metros']} m²" if a["metros"] else "⚠️ m² no indicados"
    zona = a["zona"] or "⚠️ zona no indicada"
    contacto = "\n✅ Ya tienen tu perfil" if agencia.get("contactado") else ""
    return (f"🏠 <b>Nuevo piso · {e(agencia['nombre'])}</b>\n"
            f"💶 {e(precio)} · 📐 {e(metros)}\n📍 {e(zona)}\n"
            f"{e(a['titulo'][:120])}\n{e(a['url'])}{contacto}")


def revisar_agencia(agencia: dict, cfg: dict, vistos: dict) -> tuple[list, list, str | None]:
    """Devuelve (anuncios a avisar, todas las urls vistas, error)."""
    r = cfg["rastreo"]
    todas, nuevos = [], []
    previos = set(vistos.get(agencia["id"], {}).get("urls", []))
    primera_vez = agencia["id"] not in vistos
    try:
        for url in agencia["urls"]:
            anuncios = extraer_anuncios(descargar(url, r["timeout_segundos"], agencia["modo"]), url)
            todas += [a["url"] for a in anuncios]
            nuevos += [a for a in anuncios if a["url"] not in previos]
    except Exception as e:
        return [], [], f"{type(e).__name__}: {str(e)[:150]}"
    if primera_vez:  # primera pasada: guardamos lo que ya había sin avisar
        return [], todas, None
    avisos = []
    for i, a in enumerate(nuevos):
        ev = evaluar(a, cfg, r["timeout_segundos"], agencia["modo"],
                     r.get("abrir_fichas", True) and i < r.get("max_fichas_por_agencia", 15),
                     agencia.get("en_zona", False))
        if ev["motivo_descarte"]:
            print(f"  - descartado ({ev['motivo_descarte']}): {a['url']}")
        else:
            try:  # fotos, teléfono y formulario de la ficha
                html_ficha = ev.get("_html") or descargar(a["url"], r["timeout_segundos"], agencia["modo"])
                ev["detalle"] = contacto.extraer_detalle(html_ficha, a["url"])
            except Exception:
                ev["detalle"] = {}
            ev.pop("_html", None)
            avisos.append(ev)
    return avisos, todas, None


def cargar_tg() -> dict:
    tg = cargar_json(TELEGRAM, {"offset": 0, "pendientes": {}})
    limite = time.time() - 30 * 86400  # los botones de anuncios de hace más de un mes caducan
    tg["pendientes"] = {k: v for k, v in tg.get("pendientes", {}).items() if v.get("fecha", 0) > limite}
    return tg


def guardar_tg(tg: dict) -> None:
    if cargar_json(TELEGRAM, None) != tg:
        guardar_json(TELEGRAM, tg)


def ciclo(cfg: dict) -> int:
    tg_estado = cargar_tg()
    agencias = cargar_agencias()
    vistos, fallos = cargar_json(VISTOS, {}), cargar_json(FALLOS, {})
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · {len(agencias)} agencias vigiladas")
    total = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futuros = {ex.submit(revisar_agencia, ag, cfg, vistos): ag for ag in agencias}
        for fut in as_completed(futuros):
            ag = futuros[fut]
            avisos, urls, error = fut.result()
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
            reg = vistos.setdefault(ag["id"], {"urls": []})
            reg["urls"] = list(dict.fromkeys(urls + reg["urls"]))[:3000]
            for a in avisos:
                contacto.notificar(mensaje(ag, a), a["url"], a.get("detalle") or {}, tg_estado["pendientes"],
                                   {"titulo": a["titulo"][:120], "agencia": ag["nombre"]})
                total += 1
            print(f"✓ {ag['nombre']}: {len(urls)} anuncios, {len(avisos)} avisos")
    guardar_json(VISTOS, vistos)
    guardar_json(FALLOS, fallos)
    guardar_tg(tg_estado)
    print(f"Avisos enviados: {total}")
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
    codigo = ciclo(cfg)
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
