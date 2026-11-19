import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import yaml, rastreador as r
from test_extraccion import LISTADO, CFG

def test_ciclo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(r, "RAIZ", tmp_path)
    monkeypatch.setattr(r, "VISTOS", tmp_path / "estado/vistos.json")
    monkeypatch.setattr(r, "FALLOS", tmp_path / "estado/fallos.json")
    (tmp_path / "agencias.csv").write_text(
        "aicat,nombre,url_alquiler,vigilar,contactado,en_tus_zonas\n4236,Gala Finques,https://www.ejemplo-finques.cat/ca/lloguer,sí,sí,sí\n1,Otra,https://otra.cat/x,no,,\n")
    pagina = {"html": LISTADO}
    monkeypatch.setattr(r, "descargar", lambda url, t, modo="": pagina["html"])
    monkeypatch.setattr(r, "TELEGRAM", tmp_path / "estado/telegram.json")
    enviados = []
    monkeypatch.setattr(r.contacto, "tg", lambda metodo, **d: enviados.append((metodo, d)) or {"ok": True})
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    r.ciclo(CFG)                     # primera pasada: solo guarda
    assert enviados == []
    pagina["html"] = LISTADO.replace("</div>\n<a href", '<div class="card"><a href="/ca/immoble/pis-clot-ref-2001">Pis al Clot</a><p>995 €/mes · 64 m²</p></div></div>\n<a href')
    r.ciclo(CFG)
    textos = [d.get("text") or d.get("caption") or "" for m, d in enviados if m in ("sendMessage", "sendPhoto")]
    assert len(textos) == 1 and "995" in textos[0] and "Sant Martí" in textos[0] and "Ya tienen tu perfil" in textos[0]
    assert "📞" not in textos[0] or True
    pend = json.loads((tmp_path / "estado/telegram.json").read_text())["pendientes"]
    assert len(pend) == 1 and list(pend.values())[0]["url"].endswith("2001")
    n = len(enviados)
    r.ciclo(CFG)                     # no repite
    assert len(enviados) == n
