import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import yaml, rastreador as r

CFG = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text())

LISTADO = """
<html><body>
<header><a href="/ca/inici">Inici</a><a href="/ca/lloguer">Lloguer</a></header>
<nav><a href="/ca/venda">Venda</a></nav>
<div class="results">
  <div class="card"><a href="/ca/immoble/pis-gracia-ref-1234"><img alt="foto"></a>
    <h3><a href="/ca/immoble/pis-gracia-ref-1234">Pis a Vila de Gràcia</a></h3>
    <p>950 €/mes · 72 m² · 3 hab.</p></div>
  <div class="card"><a href="/ca/immoble/atic-sarria-ref-1240">Àtic a Sarrià</a>
    <p>1.350 €/mes · 90 m²</p></div>
  <div class="card"><a href="/ca/immoble/pis-sant-andreu-ref-1251">Pis Sant Andreu</a>
    <p>890 € al mes · 48 m2</p></div>
  <div class="card"><a href="/ca/immoble/pis-poblenou-ref-1299">Pis Poblenou</a>
    <p>Lloguer 980 € · 65 m2</p></div>
  <div class="card"><a href="/ca/immoble/pis-sants-ref-1300">Pis a Sants</a>
    <p>900 €/mes · 70 m2</p></div>
  <div class="card"><a href="/ca/immoble/pis-gracia-venda-ref-1301">Pis Gràcia</a>
    <p>320.000 € · 80 m2</p></div>
</div>
<a href="/ca/lloguer?page=2">2</a>
<footer>Gala Finques · Nil Fabra 13, 08012 Gràcia · <a href="/aviso-legal">Avís</a></footer>
</body></html>
"""

def test_extrae_y_filtra():
    an = r.extraer_anuncios(LISTADO, "https://www.ejemplo-finques.cat/ca/lloguer")
    urls = sorted(a["url"] for a in an)
    assert len(an) == 6, urls
    res = {a["url"].rsplit("-", 1)[-1]: r.evaluar(a, CFG, 5, "", abrir=False) for a in an}
    assert res["1234"]["motivo_descarte"] is None and res["1234"]["precio"] == 950 and res["1234"]["metros"] == 72
    assert res["1234"]["zona"] == "Gràcia"
    assert "precio" in res["1240"]["motivo_descarte"]
    assert "m²" in res["1251"]["motivo_descarte"]
    assert res["1299"]["motivo_descarte"] is None and res["1299"]["zona"] == "Sant Martí"
    assert res["1300"]["motivo_descarte"] is None and res["1300"]["zona"] is None  # avisa con ⚠️ zona
    assert "venta" in res["1301"]["motivo_descarte"]

def test_precios():
    assert r.extraer_precio("Precio: 1.000 €/mes") == 1000
    assert r.extraer_precio("€ 975 /mes") == 975
    assert r.extraer_precio("Fianza 50 € ... 1.100,00 € al mes") == 1100
    assert r.extraer_precio("850 euros mensuales") == 850
    assert r.extraer_metros("Superficie 75 m² · terraza 10 m²") == 75

def test_exclusion():
    z, ex = r.detectar_zona("Piso en Sant Andreu de la Barca", CFG)
    assert ex
    z, ex = r.detectar_zona("Bonito piso en Vallcarca", CFG)
    assert z == "Vallcarca" and not ex

def test_agencia_fuera_de_zona():
    an = r.extraer_anuncios(LISTADO, "https://www.ejemplo-finques.cat/ca/lloguer")
    sants = next(a for a in an if a["url"].endswith("1300"))
    assert r.evaluar(sants, CFG, 5, "", abrir=False, agencia_en_zona=True)["motivo_descarte"] is None
    assert "fuera" in r.evaluar(sants, CFG, 5, "", abrir=False, agencia_en_zona=False)["motivo_descarte"]
