import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import descubrir as d, rastreador as r

DDG = '''<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.idealista.com%2Fpro%2Fgala-finques%2F&rut=x">Gala Finques - idealista</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.galafinques.com%2Fca%2F&rut=y">Gala Finques | Immobiliària a Gràcia</a></div>'''
BING = '<ol><li class="b_algo"><h2><a href="https://www.finquesboada.com/">Finques Boada - Sagrera</a></h2></li></ol>'

def test_parsers_y_eleccion():
    res = d.parsear_ddg(DDG)
    assert res[1][0] == "https://www.galafinques.com/ca/"
    web, p = d.elegir_web(res, "Gala Finques")
    assert web == "https://www.galafinques.com/" and p >= 1
    assert d.parsear_bing(BING)[0][0] == "https://www.finquesboada.com/"
    assert d.elegir_web([("https://www.fotocasa.es/x", "Gala Finques")], "Gala Finques")[0] is None

def test_marcas():
    assert d.clave_marca("Estudi Sanllehy SLU (Tecnocasa)") == "tecnocasa"
    assert d.clave_marca("Punt Sant Cugat SL (La Casa Agency)") == "casa agency"
    assert d.es_persona("Teixidó Royo, Jordi") and not d.es_persona("Sansalvador Margarit, Esteban (Invest 92)")

def test_pagina_alquiler():
    html = '''<a href="/ca/venda">Venda</a><a href="/ca/lloguer">Lloguer</a><a href="/es/alquiler-pisos-barcelona">Pisos de alquiler</a>
    <a href="https://otra.com/alquiler">x</a>'''
    pags = d.paginas_alquiler(html, "https://www.galafinques.com/")
    assert pags[0] == "https://www.galafinques.com/es/alquiler-pisos-barcelona" and len(pags) == 2

def test_mis_agencias_y_duplicados(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "RAIZ", tmp_path)
    (tmp_path / "agencias.csv").write_text(
        "aicat,nombre,url_alquiler,vigilar,contactado,en_tus_zonas\n"
        "1,A SL (Tecnocasa),https://tecnocasa.es/alquiler,sí,,\n2,B SL (Tecnocasa),https://tecnocasa.es/alquiler,sí,,sí\n"
        "3,Otra (Gala),https://gala.cat/lloguer,sí,,\n4,Nueva (X),,,,\n")
    (tmp_path / "mis_agencias.csv").write_text(
        "aicat,url_alquiler,vigilar,contactado\n3,,no,sí\n4,https://x.cat/rent,sí,\n")
    ags = {a["nombre"]: a for a in r.cargar_agencias()}
    assert set(ags) == {"Tecnocasa (2 oficinas)", "X"}
    assert ags["Tecnocasa (2 oficinas)"]["en_zona"] is True

def test_botones_autorizados(monkeypatch):
    import contacto as c
    llamadas = []
    monkeypatch.setattr(c, "tg", lambda m, **d: llamadas.append((m, d)) or {})
    monkeypatch.setattr(c, "enviar_formulario", lambda url, titulo: (True, "Datos enviados"))
    pend = {"abc": {"url": "https://x.cat/p/1", "titulo": "Piso", "agencia": "Gala", "fecha": 1}}
    c.atender({"id": "1", "data": "i:abc", "message": {"message_id": 5}}, "-100", pend)
    assert "Sí, enviar" in str(llamadas[-1])
    c.atender({"id": "2", "data": "s:abc", "message": {"message_id": 6}, "from": {"first_name": "M"}}, "-100", pend)
    assert pend["abc"]["estado"] == "enviado" and "✅" in llamadas[-1][1]["text"]
    c.atender({"id": "3", "data": "s:abc", "message": {"message_id": 6}}, "-100", pend)
    assert "Ya se ha enviado" in str(llamadas[-1])
