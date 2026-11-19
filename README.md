# Rastreador de pisos · agencias AICAT de Barcelona

Revisa cada 5 minutos la página de alquileres de las agencias que tú elijas y te manda un Telegram en cuanto aparece un piso nuevo con:

- **60 m² o más**
- **1.000 €/mes o menos**
- en **Sant Andreu, Sant Martí, Sarrià-Sant Gervasi, Gràcia o Vallcarca**

Los filtros están en `config.yaml` y se pueden cambiar en cualquier momento.

> Las webs de las agencias no tienen API: el rastreador lee su página como lo haría un navegador. Por eso hay que decirle, agencia a agencia, cuál es su página de alquileres.

---

## Puesta en marcha (unos 15 minutos, una sola vez)

### 1. Crea tu bot de Telegram

1. En Telegram, abre **@BotFather** y envía `/newbot`. Ponle un nombre (por ejemplo *Pisos BCN*).
2. BotFather te da un **token** (algo como `7123456789:AAH...`). Guárdalo.
3. Abre tu nuevo bot y envíale cualquier mensaje («hola»).
4. En el navegador abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca `"chat":{"id":123456789`. Ese número es tu **chat id**.

### 1b. Para recibir los avisos entre dos (o más)

Telegram no deja que un bot escriba a un número de teléfono: cada persona tiene que poder recibir mensajes del bot. Hay dos formas:

**Opción A: un grupo (recomendada).** Los dos veis los mismos avisos en un solo sitio y podéis comentarlos.
1. Crea un grupo de Telegram con tu pareja (por ejemplo «Pisos BCN») y añade el bot como miembro.
2. Escribe cualquier mensaje en el grupo.
3. Abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca el `"chat":{"id":-100...`. El id del grupo empieza por `-`.
4. Usa ese número como `TELEGRAM_CHAT_ID`.

**Opción B: cada uno en su chat.** Cada persona abre el bot y le escribe un mensaje; con `getUpdates` sacáis los dos ids y los ponéis juntos, separados por coma, en `TELEGRAM_CHAT_ID` (por ejemplo `123456789,987654321`).

Si `getUpdates` sale vacío, manda otro mensaje y recarga. En un grupo, si el bot no ve los mensajes, en @BotFather → `/setprivacy` → elige el bot → **Disable**.

### 2. Sube el proyecto a GitHub

1. Crea una cuenta en github.com si no tienes.
2. **New repository** → nombre `rastreador-pisos` → **Public** → Create.
   - Público porque así GitHub Actions es gratis e ilimitado. En un repositorio privado, revisar cada 5 minutos gasta los 2.000 minutos gratis del mes en unos 10 días. Lo único visible es la lista de agencias y las URL de anuncios; el token de Telegram queda secreto.
3. **Add file → Upload files** y arrastra todo el contenido de esta carpeta (incluida la carpeta `.github`). Commit.

### 3. Añade el token y el chat id como secretos

**Settings → Secrets and variables → Actions → New repository secret**:

| Nombre | Valor |
| --- | --- |
| `TELEGRAM_TOKEN` | el token de BotFather |
| `TELEGRAM_CHAT_ID` | tu chat id, el del grupo, o varios separados por comas |
| `CONTACTO_DATOS` | vuestros datos para los formularios (ver 3b) |

### 3b. Tus datos para los formularios (cifrados)

Para que el botón **✉️ Enviar mis datos** pueda rellenar el formulario del anuncio, crea un secreto más con el nombre `CONTACTO_DATOS`. Su valor es este texto con vuestros datos:

```json
{
  "nombre": "Marina",
  "apellidos": "Tu apellido",
  "email": "tu@correo.com",
  "telefono": "600000000",
  "mensaje": "Hola, somos una pareja interesada en el piso {titulo}. Tenemos contrato indefinido y la documentación de solvencia preparada. ¿Podríamos visitarlo? Gracias. ({url})"
}
```

- `{titulo}` y `{url}` se sustituyen por los del anuncio.
- GitHub guarda los secretos cifrados: una vez guardado, nadie puede volver a verlo, ni siquiera tú desde la web (solo sustituirlo). El bot lo descifra únicamente en el momento de rellenar un formulario y oculta cada valor en los registros.
- Tus datos nunca se escriben en el repositorio. Así, aunque el repositorio sea público, no se ven.

### 4. Activa el rastreador

1. **Settings → Actions → General → Workflow permissions → Read and write permissions** → Save.
2. Pestaña **Actions** → si lo pide, *I understand my workflows, go ahead and enable them*.
3. **Probar una web → Run workflow**, pon cualquier URL y `telegram: sí`. Si te llega «✅ El rastreador de pisos está conectado a Telegram», todo funciona.

Desde ese momento **Rastreador de pisos** se ejecuta solo cada 5 minutos.

---

## Las webs de las agencias se buscan solas

El directorio AICAT no publica las webs, así que la tarea **Descubrir webs de agencias** las busca automáticamente:

- Se ejecuta cada hora en tandas de unos 50 minutos. Tarda alrededor de un día en recorrer las ~2.700 marcas; las oficinas de una misma cadena (las 166 Tecnocasa, las La Casa Agency…) cuentan como una sola.
- Para cada marca busca su web, comprueba que es una inmobiliaria y localiza su página de alquileres.
- Si el rastreador detecta pisos en esa página, rellena `url_alquiler` y pone `vigilar = sí` en `agencias.csv`. Desde ese momento el rastreador la revisa cada 5 minutos.
- Empieza por las agencias de tus barrios y por las cadenas grandes.
- Los agentes que figuran solo con nombre de persona (sin nombre comercial) se saltan: casi nunca tienen web propia.
- Para empezar ya, sin esperar a la hora en punto: **Actions → Descubrir webs de agencias → Run workflow**.
- El resultado de cada marca (web encontrada, dudosa, sin web…) queda en `datos/descubrimiento.json`.

Usa buscadores web gratuitos (DuckDuckGo y Bing). Si alguno bloquea las búsquedas, la tarea se detiene y sigue en la hora siguiente. Para hacerlo más rápido y fiable puedes añadir el secreto `BRAVE_API_KEY` con una clave gratuita de Brave Search API (2.000 búsquedas al mes).

## Día a día: el panel de agencias

Usa el panel **Agencias AICAT · Área de Barcelona** (la página que te he publicado). Tiene todas las agencias AICAT de Barcelona ciudad y de los municipios vecinos (L'Hospitalet, Badalona, Sant Cugat, Esplugues, Sant Just, Cornellà, Santa Coloma, Sant Adrià, Montcada, Sant Joan Despí, Sant Feliu, El Prat y Montgat). Los filtros *Tu zona / Resto de Barcelona / Área metropolitana* indican dónde tiene la oficina cada agencia; el filtro de barrio se aplica siempre al piso, no a la agencia.

1. **Ver lo descubierto**: descarga `agencias.csv` de tu repositorio (botón *Download raw file*) y pulsa **Importar webs de GitHub** en el panel. Las webs y páginas encontradas solas salen en cursiva.
2. Marca **Contactada** cuando una agencia ya tenga tu perfil. Los avisos de esas agencias llevan «✅ Ya tienen tu perfil».
3. Corrige lo que esté mal: cambia una URL, añade una que faltaba o quita **Vigilar**.
4. Pulsa **Exportar mis cambios** y sube `mis_agencias.csv` a GitHub (*Add file → Upload files*). Solo lleva lo que tú has tocado, así que nunca borra lo descubierto.

### ¿Funcionará con la web de esta agencia?

Antes de añadirla, compruébalo: **Actions → Probar una web → Run workflow** con su URL. En el resultado verás qué anuncios detecta, con precio, metros y zona, y si te avisaría o no.

- **Si detecta 0 anuncios**, la web carga los pisos con JavaScript. Vuelve a probar con `modo: js`. Si así funciona, pon `js` en la columna `modo` de esa agencia.
- **Si sigue sin detectar nada**, la web bloquea robots o es muy atípica. Esa agencia, mejor por contacto directo: márcala como contactada.

---

## Qué trae cada aviso

- Hasta 4 fotos del piso, precio, metros, barrio y título.
- 📞 Teléfono de la agencia y 💬 enlace de WhatsApp, si aparecen en la ficha.
- **🔗 Ver anuncio**.
- **✉️ Enviar mis datos**, si la ficha tiene formulario de contacto:
  1. Al pulsarlo, el bot pregunta «¿Envío vuestros datos a <agencia>?» y hay que confirmar con **✅ Sí, enviar**. Así un toque sin querer no envía nada.
  2. Abre la ficha en un navegador, rellena nombre, email, teléfono y mensaje, acepta la política de privacidad (nunca la newsletter ni publicidad) y envía.
  3. Contesta en el chat si ha ido bien, o por qué no (por ejemplo, si el formulario pide un dato que no tiene).
  - Cada anuncio solo se envía una vez, aunque lo pulséis los dos.
  - Si el formulario tiene **captcha**, el bot no lo intenta resolver: el aviso lo indica y lo enviáis vosotros desde el enlace.
  - La respuesta tarda de unos segundos a unos minutos: el bot atiende los botones entre revisión y revisión.
  - Para probar una web sin enviar nada: **Actions → Probar una web**, con `formulario: sí`.

## Cómo decide si avisarte

- **Primera revisión de una agencia:** guarda los anuncios que ya hay sin avisarte, para que no te lleguen 40 mensajes de golpe. A partir de ahí, solo te avisa de los nuevos.
- **Precio y metros:** los lee de la tarjeta del anuncio. Si faltan, abre la ficha.
  - Más de 10.000 € se considera venta y se descarta.
  - Si no encuentra el precio o los metros, te avisa igualmente con ⚠️, para que no se te escape nada.
- **Zona:** busca nombres de barrio (Gràcia, Vallcarca, Poblenou, Sagrera, Sant Gervasi, Galvany, Putxet…) o el código postal.
  - Si no encuentra ninguno y la agencia tiene la oficina en tus distritos, te avisa con «⚠️ zona no indicada».
  - Si la agencia está en otra zona y el anuncio no dice el barrio, lo descarta, para no llenarte de pisos de otros barrios.
  - Para descartar siempre los anuncios sin barrio, pon `avisar_si_zona_desconocida: false` en `config.yaml`.
- **Si la web de una agencia falla 6 veces seguidas**, te llega un aviso para que revises la URL.

## Límites que conviene saber

- GitHub no garantiza los 5 minutos exactos: en horas de mucha carga, una ejecución puede retrasarse de 5 a 15 minutos. Sigue siendo mucho antes de que el piso llegue a Idealista.
- Cada web es distinta. El detector reconoce la mayoría de plantillas de agencia (Inmovilla, Witei, WordPress y similares), pero conviene probar cada URL una vez.
- GitHub pausa las tareas programadas de un repositorio sin actividad durante 60 días. Aquí no pasará, porque el rastreador guarda su estado con commits.

## Archivos

| Archivo | Para qué |
| --- | --- |
| `rastreador.py` | El programa |
| `contacto.py` | Fotos, teléfono, botones de Telegram y relleno de formularios |
| `config.yaml` | Filtros: metros, precio, barrios, códigos postales |
| `agencias.csv` | Las 3.761 agencias AICAT de Barcelona y municipios vecinos, con las webs que se van descubriendo |
| `mis_agencias.csv` | Tus cambios exportados desde el panel (contactadas, URLs corregidas, vigilar sí/no) |
| `descubrir.py` | El buscador automático de webs |
| `estado/vistos.json` | Anuncios ya vistos (lo actualiza el propio rastreador) |
| `estado/telegram.json` | Anuncios avisados y si ya se enviaron los datos (sin datos personales) |
| `.github/workflows/` | Las tareas automáticas de GitHub |
| `datos/` | Datos originales del directorio AICAT y generador del panel |
| `tests/` | Pruebas del detector |

Prueba en local (opcional): `pip install -r requirements.txt` y `python rastreador.py --probar https://web-de-la-agencia/alquiler`.
