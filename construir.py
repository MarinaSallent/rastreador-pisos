"""Genera agencias.csv (rastreador) y app/agencias.html (panel) a partir de los datos del directorio AICAT."""
import csv, json, pathlib
R = pathlib.Path(__file__).resolve().parents[1]
D = R / "datos"
TUS_CP = {"08005","08006","08012","08017","08018","08019","08020","08021","08022","08023","08024","08025",
          "08026","08027","08030","08033","08034","08037"}
BCN = {"08001":"Ciutat Vella","08002":"Ciutat Vella","08003":"Ciutat Vella","08004":"Sants-Montjuïc","08005":"Sant Martí",
 "08006":"Sant Gervasi / Gràcia","08007":"Eixample","08008":"Eixample","08009":"Eixample","08010":"Eixample","08011":"Eixample",
 "08012":"Gràcia","08013":"Eixample","08014":"Sants-Montjuïc","08015":"Eixample / Sants","08016":"Nou Barris","08017":"Sarrià",
 "08018":"Sant Martí","08019":"Sant Martí","08020":"Sant Martí","08021":"Sant Gervasi","08022":"Sant Gervasi",
 "08023":"Vallcarca / Gràcia","08024":"Gràcia","08025":"Gràcia / Eixample","08026":"Sant Martí / Eixample","08027":"Sant Andreu",
 "08028":"Les Corts / Sants","08029":"Eixample / Les Corts","08030":"Sant Andreu / Sant Martí","08031":"Nou Barris / Horta",
 "08032":"Horta-Guinardó","08033":"Sant Andreu / Nou Barris","08034":"Sarrià / Pedralbes","08035":"Horta-Guinardó",
 "08036":"Eixample","08037":"Gràcia / Eixample","08038":"Sants-Montjuïc","08039":"Ciutat Vella","08040":"Zona Franca",
 "08041":"Horta-Guinardó","08042":"Nou Barris"}
MUN = {"SANT ADRIÀ":"Sant Adrià de Besòs","STA COLOMA":"Santa Coloma de Gramenet","MONTCADA":"Montcada i Reixac",
       "SANT CUGAT":"Sant Cugat del Vallès","CORNELLÀ":"Cornellà de Llobregat","SANT FELIU":"Sant Feliu de Llobregat",
       "EL PRAT":"El Prat de Llobregat","SANT JOAN DESPÍ":"Sant Joan Despí","MONTGAT":"Montgat",
       "L'HOSPITALET DE LLOBREGAT":"L'Hospitalet de Llobregat","ESPLUGUES DE LLOBREGAT":"Esplugues de Llobregat",
       "SANT JUST DESVERN":"Sant Just Desvern","BADALONA":"Badalona","SANT ADRIÀ DE BESÒS":"Sant Adrià de Besòs"}
filas, vistos = [], set()
def leer(nombre, municipio=None):
    for l in (D / nombre).read_text(encoding="utf-8").splitlines():
        if not l.strip(): continue
        p = l.split("|")
        a, n, d, cp = p[0].strip(), p[1].strip(), p[2].strip(), p[3].strip()
        mun = MUN.get(p[4].strip(), p[4].strip().title()) if len(p) > 4 else municipio
        if not a.isdigit() or a in vistos or not n: continue
        vistos.add(a); filas.append((int(a), n, d, cp, mun))
for f in ["aicat_raw.txt", "bcn_resto_1.txt", "bcn_resto_2.txt", "bcn_resto_3.txt"]:
    leer(f, "Barcelona")
for f in ["metro_1.txt", "metro_2.txt"]:
    leer(f)
filas.sort()
def zona(cp, mun):
    return BCN.get(cp, "Barcelona") if mun == "Barcelona" else mun
with open(R / "agencias.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["aicat","nombre","direccion","cp","municipio","zona","en_tus_zonas","web","url_alquiler","vigilar","modo",
                "contactado","fecha_contacto","notas"])
    for a, n, d, cp, mun in filas:
        w.writerow([a, n, d, cp, mun, zona(cp, mun), "sí" if (mun == "Barcelona" and cp in TUS_CP) else "",
                    "", "", "", "", "", "", ""])
datos = [[a, n, d, cp, mun] for a, n, d, cp, mun in filas]
html = (D / "plantilla.html").read_text(encoding="utf-8")
html = (html.replace("__DATOS__", json.dumps(datos, ensure_ascii=False, separators=(",", ":")))
            .replace("__BCN__", json.dumps(BCN, ensure_ascii=False))
            .replace("__TUSCP__", json.dumps(sorted(TUS_CP))))
(R / "app").mkdir(exist_ok=True)
(R / "app/agencias.html").write_text(html, encoding="utf-8")
from collections import Counter
print(len(filas), "agencias;", Counter(m for *_, m in filas).most_common())
print("en tus zonas:", sum(1 for a, n, d, cp, m in filas if m == "Barcelona" and cp in TUS_CP))
