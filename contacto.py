"""
Fotos, teléfonos y formulario de contacto de un anuncio, y los botones de Telegram para usarlo.

Flujo:
  1. Al avisar de un piso, el mensaje lleva fotos, teléfono/WhatsApp de la agencia y, si la ficha
     tiene formulario de contacto, el botón «✉️ Enviar mis datos».
  2. Al pulsarlo, el bot pide confirmación («✅ Sí, enviar» / «❌ Cancelar»).
  3. Al confirmar, abre la ficha en un navegador real, rellena el formulario con tus datos
     (secreto CONTACTO_DATOS de GitHub, cifrado) y lo envía. Responde en el chat si ha ido bien.

Tus datos nunca se escriben en el repositorio ni en los registros: solo se leen de la variable
de entorno en el momento de rellenar el formulario.

Si el formulario tiene un captcha, el bot NO lo intenta resolver: te avisa para que lo envíes tú.
"""
from __future__ import annotations

import hashlib
import html as H
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

RE_TEL = re.compile(r"(?<![\d/])(?:\+?34[\s.\-]?)?([6789](?:[\s.\-]?\d){8})(?![\d/])")
NO_FOTO = re.compile(r"(logo|icon|sprite|favicon|placeholder|blank|loading|avatar|flag|bandera|banner|\.svg|"
                     r"pixel|tracking|captcha|mapa|staticmap|marker|whatsapp|facebook|instagram|twitter|linkedin)",
                     re.I)
CAPTCHA_SEL = ".g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*='recaptcha/api2/anchor'], iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare']"


# ---------------------------------------------------------------- datos de la ficha
def extraer_detalle(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fotos: list[str] = []

    def add(src: str | None):
        if not src:
            return
        src = urljoin(url, src.strip().split(" ")[0])
        if src.startswith("http") and not NO_FOTO.search(src) and src not in fotos:
            fotos.append(src)

    for m in soup.select("meta[property='og:image'], meta[name='og:image'], meta[property='og:image:url']"):
        add(m.get("content"))
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("data-original") or img.get("src")
        w = img.get("width") or ""
        if w.isdigit() and int(w) < 200:
            continue
        if src and re.search(r"\.(jpe?g|webp|png)(\?|$)", src, re.I):
            add(src)
        if len(fotos) >= 6:
            break

    telefonos: list[str] = []
    for a in soup.select("a[href^='tel:']"):
        n = re.sub(r"\D", "", a["href"])[-9:]
        if len(n) == 9 and n[0] in "6789" and n not in telefonos:
            telefonos.append(n)
    if not telefonos:
        for m in RE_TEL.finditer(soup.get_text(" ", strip=True)):
            n = re.sub(r"\D", "", m.group(1))
            if n not in telefonos:
                telefonos.append(n)
    whatsapp = next((a["href"] for a in soup.select("a[href*='wa.me'], a[href*='api.whatsapp.com']")), None)

    captcha = bool(soup.select(".g-recaptcha, .h-captcha, .cf-turnstile, [data-hcaptcha-sitekey]"))
    formulario = False
    for f in soup.find_all("form"):
        campos = " ".join(f"{i.get('type','')} {i.get('name','')} {i.get('id','')} {i.get('placeholder','')}"
                          for i in f.find_all(["input", "textarea"]))
        if re.search(r"(email|mail|correo)", campos, re.I) and (f.find("textarea") or re.search(r"(tel|phone)", campos, re.I)):
            formulario = True
            captcha |= bool(f.select(".g-recaptcha, .h-captcha, .cf-turnstile"))
    boton_contacto = bool(re.search(r"(contactar|me interesa|m'interessa|solicitar informaci|pedir visita|demanar)",
                                    soup.get_text(" ", strip=True)[:50000], re.I))
    return {"fotos": fotos[:4], "telefonos": telefonos[:2], "whatsapp": whatsapp,
            "formulario": formulario or boton_contacto, "captcha": captcha}


def formatear_tel(n: str) -> str:
    return f"{n[:3]} {n[3:5]} {n[5:7]} {n[7:]}" if len(n) == 9 else n


# ---------------------------------------------------------------- Telegram
def _token() -> str | None:
    return os.environ.get("TELEGRAM_TOKEN")


def chats() -> list[str]:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def tg(metodo: str, **datos):
    tok = _token()
    if not tok:
        print(f"[telegram desactivado] {metodo}: {str(datos.get('text') or datos.get('caption') or '')[:200]}")
        return None
    for k, v in list(datos.items()):
        if isinstance(v, (dict, list)):
            datos[k] = json.dumps(v)
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/{metodo}", data=datos, timeout=60)
        except requests.RequestException as e:
            print(f"Telegram {metodo}: {e}", file=sys.stderr)
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)))
            continue
        if not r.ok:
            print(f"Telegram {metodo} {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
        return r.json().get("result")
    return None


def id_anuncio(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def notificar(texto: str, url: str, detalle: dict, pendientes: dict, info: dict) -> None:
    """Fotos + mensaje con botones, a cada chat configurado."""
    aid = id_anuncio(url)
    extra = []
    if detalle.get("telefonos"):
        extra.append("📞 " + " · ".join(formatear_tel(t) for t in detalle["telefonos"]))
    if detalle.get("whatsapp"):
        extra.append(f'💬 <a href="{H.escape(detalle["whatsapp"])}">WhatsApp</a>')
    botones = [[{"text": "🔗 Ver anuncio", "url": url}]]
    if detalle.get("formulario") and not detalle.get("captcha"):
        botones.insert(0, [{"text": "✉️ Enviar mis datos", "callback_data": f"i:{aid}"}])
    elif detalle.get("captcha"):
        extra.append("✍️ El formulario tiene captcha: envíalo tú desde el anuncio.")
    texto_final = texto + ("\n" + "\n".join(extra) if extra else "")
    pendientes[aid] = {**info, "url": url, "estado": "avisado", "fecha": int(time.time())}

    for chat in chats() or [None]:
        fotos = detalle.get("fotos") or []
        if chat and len(fotos) >= 2:
            ok = tg("sendMediaGroup", chat_id=chat,
                    media=[{"type": "photo", "media": f} for f in fotos[:4]])
            if not ok:  # algunas webs no dejan que Telegram descargue sus fotos
                fotos = []
        if chat and len(fotos) == 1:
            if tg("sendPhoto", chat_id=chat, photo=fotos[0], caption=texto_final[:1024], parse_mode="HTML",
                  reply_markup={"inline_keyboard": botones}):
                continue
        tg("sendMessage", chat_id=chat, text=texto_final, parse_mode="HTML",
           reply_markup={"inline_keyboard": botones}, disable_web_page_preview="true")
        time.sleep(1.1)


# ---------------------------------------------------------------- escuchar los botones
def escuchar(estado: dict, hasta: float) -> None:
    """Atiende pulsaciones de botones hasta la hora indicada (long polling)."""
    permitidos = set(chats())
    if not _token() or not permitidos:
        return
    pendientes = estado.setdefault("pendientes", {})
    while time.time() < hasta - 5:
        espera = int(max(1, min(50, hasta - time.time() - 5)))
        upd = tg("getUpdates", offset=estado.get("offset", 0), timeout=espera,
                 allowed_updates=["callback_query"]) or []
        for u in upd:
            estado["offset"] = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq:
                continue
            chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            if chat not in permitidos:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="No autorizado")
                continue
            atender(cq, chat, pendientes)
        if not upd:
            time.sleep(1)


def atender(cq: dict, chat: str, pendientes: dict) -> None:
    accion, _, aid = (cq.get("data") or "").partition(":")
    p = pendientes.get(aid)
    quien = cq.get("from", {}).get("first_name", "")
    if not p:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ese anuncio ya no está en la lista")
        return
    titulo = H.escape(p.get("titulo", "")[:80])
    if accion == "i":
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        if p.get("estado") == "enviado":
            tg("sendMessage", chat_id=chat, text=f"Ya enviasteis los datos para «{titulo}».", parse_mode="HTML")
            return
        tg("sendMessage", chat_id=chat, parse_mode="HTML",
           text=f"¿Envío vuestros datos a <b>{H.escape(p.get('agencia', ''))}</b> para «{titulo}»?",
           reply_markup={"inline_keyboard": [[{"text": "✅ Sí, enviar", "callback_data": f"s:{aid}"},
                                              {"text": "❌ Cancelar", "callback_data": f"c:{aid}"}]]})
    elif accion == "c":
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Cancelado")
        tg("editMessageText", chat_id=chat, message_id=cq["message"]["message_id"], text="Cancelado.")
    elif accion == "s":
        if p.get("estado") in ("enviado", "enviando"):
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Ya se ha enviado")
            return
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Enviando…")
        tg("editMessageText", chat_id=chat, message_id=cq["message"]["message_id"],
           text=f"⏳ Rellenando el formulario de «{p.get('titulo', '')[:80]}»…")
        p["estado"] = "enviando"
        try:
            ok, detalle = enviar_formulario(p["url"], p.get("titulo", ""))
        except Exception as e:
            ok, detalle = False, f"error inesperado ({type(e).__name__})"
        p["estado"] = "enviado" if ok else "fallido"
        p["resultado"] = detalle
        p["enviado_por"] = quien
        icono = "✅" if ok else "⚠️"
        tg("sendMessage", chat_id=chat, parse_mode="HTML", disable_web_page_preview="true",
           text=f"{icono} {H.escape(detalle)}\n«{titulo}»\n{H.escape(p['url'])}")


# ---------------------------------------------------------------- rellenar el formulario
def datos_contacto() -> dict:
    """Lee el secreto CONTACTO_DATOS (JSON) y oculta cada valor en los registros de GitHub."""
    bruto = os.environ.get("CONTACTO_DATOS", "")
    if not bruto:
        raise RuntimeError("falta el secreto CONTACTO_DATOS")
    datos = json.loads(bruto)
    if os.environ.get("GITHUB_ACTIONS"):
        for v in datos.values():
            for linea in str(v).splitlines():
                if linea.strip():
                    print(f"::add-mask::{linea.strip()}")
    return datos


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "playwright"], check=True)
    from playwright.sync_api import sync_playwright
    return sync_playwright


JS_ANALIZAR = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const desc = el => {
    const lab = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return [el.type, el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.autocomplete,
            lab ? lab.innerText : '', el.closest('label') ? el.closest('label').innerText : '']
           .join(' ').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
  };
  const forms = [...document.querySelectorAll('form')].map((f, i) => {
    const campos = [...f.querySelectorAll('input, textarea, select')].filter(e => e.type !== 'hidden');
    const d = campos.map(desc).join(' ');
    let p = 0;
    if (/mail|correo/.test(d)) p += 3;
    if (f.querySelector('textarea')) p += 2;
    if (/tel|phone|movil|mobil/.test(d)) p += 1;
    if (/password|contrasena|login|buscar|search/.test(d)) p -= 5;
    if (/newsletter|suscri/.test(d) && !f.querySelector('textarea')) p -= 5;
    if (vis(f)) p += 2;
    return {i, p, visible: vis(f)};
  }).sort((a, b) => b.p - a.p);
  return forms;
}
"""

JS_RELLENAR = r"""
([idx, datos]) => {
  const f = document.querySelectorAll('form')[idx];
  const norm = s => (s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
  const desc = el => {
    const lab = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return norm([el.type, el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.autocomplete,
                 lab ? lab.innerText : '', el.closest('label') ? el.closest('label').innerText : ''].join(' '));
  };
  const poner = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const hechos = [], sinRellenar = [];
  const todos = [...f.querySelectorAll('input, textarea, select')].filter(e => e.type !== 'hidden');
  const hayApellido = todos.some(e => { const d = desc(e); return /apellido|cognom|surname|last.?name/.test(d) && !/(nombre|nom\b|name).*(apellido|cognom)/.test(d); });
  for (const el of f.querySelectorAll('input, textarea, select')) {
    if (el.type === 'hidden' || el.disabled || el.readOnly) continue;
    const d = desc(el);
    if (el.type === 'checkbox') {
      if (/privacidad|privacitat|privacy|acepto|accepto|condicion|condicions|legal|rgpd|gdpr|terminos|termes|politica/.test(d)
          && !/newsletter|comercial|publicidad|publicitat|ofertas|ofertes|promocion|marketing/.test(d)) {
        if (!el.checked) el.click();
        hechos.push('aceptar privacidad');
      } else if (el.required && !el.checked) sinRellenar.push('casilla: ' + d.slice(0, 60));
      continue;
    }
    if (el.type === 'radio' || el.type === 'submit' || el.type === 'button' || el.type === 'file') continue;
    if (el.tagName === 'SELECT') { if (el.required && !el.value) sinRellenar.push('desplegable: ' + d.slice(0, 60)); continue; }
    let v = null, campo = null;
    if (el.tagName === 'TEXTAREA' || /mensaje|missatge|message|comentari|consulta/.test(d)) { v = datos.mensaje; campo = 'mensaje'; }
    else if (el.type === 'email' || /mail|correo/.test(d)) { v = datos.email; campo = 'email'; }
    else if (el.type === 'tel' || /tel|phone|movil|mobil/.test(d)) { v = datos.telefono; campo = 'teléfono'; }
    else if (/(nombre|\bnom\b|name).*(apellido|cognom)|full.?name|nom complet|nombre completo/.test(d)) { v = datos.nombre_completo; campo = 'nombre'; }
    else if (/apellido|cognom|surname|last.?name/.test(d)) { v = datos.apellidos || ''; campo = 'apellidos'; }
    else if (/nombre|name|\bnom\b|first/.test(d)) { v = hayApellido ? datos.nombre : datos.nombre_completo; campo = 'nombre'; }
    else if (/asunto|subject|assumpte/.test(d)) { v = 'Interesados en el inmueble'; campo = 'asunto'; }
    if (v !== null && v !== undefined && v !== '') { poner(el, v); hechos.push(campo); }
    else if (el.required && !el.value) sinRellenar.push(d.slice(0, 60));
  }
  return {hechos, sinRellenar};
}
"""

RE_OK = re.compile(r"(gracias|grac[ií]es|enviad[oa]|recibid[oa]|rebut|thank|correctamente|correctament|success|"
                   r"nos pondremos|ens posarem|en breve|aviat|we will contact|message sent|mensaje enviado)", re.I)


def enviar_formulario(url: str, titulo: str = "", datos: dict | None = None, headless: bool = True,
                      ejecutable: str | None = None, enviar: bool = True) -> tuple[bool, str]:
    datos = dict(datos or datos_contacto())
    datos.setdefault("nombre_completo", " ".join(x for x in [datos.get("nombre"), datos.get("apellidos")] if x))
    datos["mensaje"] = (datos.get("mensaje") or "Hola, estamos interesados en este inmueble. ¿Podríamos visitarlo? Gracias.") \
        .replace("{titulo}", titulo).replace("{url}", url)

    sync_playwright = _playwright()
    with sync_playwright() as p:
        opciones = {"headless": headless}
        if ejecutable:
            opciones["executable_path"] = ejecutable
        else:
            opciones["channel"] = "chrome"  # Chrome ya viene instalado en GitHub Actions
        nav = p.chromium.launch(**opciones)
        pag = nav.new_page(locale="es-ES")
        try:
            pag.goto(url, timeout=45000, wait_until="domcontentloaded")
            pag.wait_for_timeout(2500)
            for txt in ["Aceptar", "Acceptar", "Accept", "Aceptar todas", "Acceptar-les totes", "Rechazar", "Rebutjar"]:
                try:  # banner de cookies: cerrarlo para que no tape el formulario (se rechazan si se puede)
                    b = pag.get_by_role("button", name=re.compile(rf"^{txt}", re.I)).first
                    if b.is_visible(timeout=300):
                        b.click(timeout=1500)
                        break
                except Exception:
                    pass

            forms = pag.evaluate(JS_ANALIZAR)
            if not forms or forms[0]["p"] < 4 or not forms[0]["visible"]:
                abrir = pag.get_by_text(re.compile(r"(contactar|contacta|me interesa|m'interessa|solicitar inform|"
                                                   r"más información|mes informaci|pedir visita|demanar visita|"
                                                   r"enviar mensaje|request info)", re.I)).first
                try:
                    abrir.click(timeout=4000)
                    pag.wait_for_timeout(2000)
                    forms = pag.evaluate(JS_ANALIZAR)
                except Exception:
                    pass
            if not forms or forms[0]["p"] < 4:
                return False, "No he encontrado formulario de contacto en el anuncio. Llama o escribe desde el enlace."

            form = pag.locator("form").nth(forms[0]["i"])
            if form.locator(CAPTCHA_SEL).count() or pag.locator(CAPTCHA_SEL).count():
                return False, "El formulario tiene captcha y no lo relleno por ti: envíalo tú desde el enlace."

            res = pag.evaluate(JS_RELLENAR, [forms[0]["i"], datos])
            if "email" not in res["hechos"] and "teléfono" not in res["hechos"]:
                return False, "No he sabido identificar los campos del formulario. Hazlo tú desde el enlace."
            if res["sinRellenar"]:
                return False, "El formulario pide campos que no sé rellenar (" + "; ".join(res["sinRellenar"][:3]) + ")."

            if not enviar:
                return True, "Prueba: formulario encontrado y rellenado sin enviar (" + ", ".join(dict.fromkeys(res["hechos"])) + ")."
            antes = pag.url
            boton = form.locator("button[type=submit], input[type=submit]").first
            if not boton.count():
                boton = form.get_by_role("button", name=re.compile(r"(enviar|envia|send|solicitar|contactar)", re.I)).first
            boton.click(timeout=8000)
            try:
                pag.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            pag.wait_for_timeout(2500)

            texto = pag.inner_text("body")[:30000]
            invalidos = pag.evaluate("() => [...document.querySelectorAll('form :invalid')].length")
            if RE_OK.search(texto) or pag.url != antes:
                return True, "Datos enviados a la agencia (rellenado: " + ", ".join(dict.fromkeys(res["hechos"])) + ")."
            if invalidos:
                return False, "El formulario no aceptó los datos (hay campos marcados como inválidos)."
            return True, ("Formulario enviado, aunque la web no muestra confirmación. "
                          "Si en 1-2 días no os contestan, llamad.")
        finally:
            nav.close()
