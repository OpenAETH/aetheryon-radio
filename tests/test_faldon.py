from playlist_api import clean_chyron, layout_segments


def test_layout_reparte_por_peso_y_respeta_at_s():
    segs = layout_segments([{"headline": "A", "w": 1}, {"headline": "B", "w": 3}, {"headline": "C", "at_s": 99}], 40)
    assert [s["headline"] for s in segs] == ["A", "B", "C"]
    assert segs[0]["at_s"] == 0.0 and segs[1]["at_s"] == 8.0 and segs[2]["at_s"] == 40.0   # C: recortado a la duración


def test_layout_descarta_basura_y_limita_textos():
    assert layout_segments("x", 10) == [] and layout_segments([{"deck": "sin titular"}, 5], 10) == []
    assert len(layout_segments([{"headline": "h" * 500}], 10)[0]["headline"]) == 120


def test_clean_chyron():
    assert clean_chyron({"headline": "x", "otro": 1}) == {"headline": "x"} and clean_chyron([1]) is None


def test_layout_reescala_at_s_a_la_duracion_real():
    segs = layout_segments([{"headline": "A", "at_s": 0}, {"headline": "B", "at_s": 10}], 22, 20)   # estimó 20 s, el audio dura 22
    assert [s["at_s"] for s in segs] == [0.0, 11.0]
    assert layout_segments([{"headline": "A", "at_s": 10}], 30, "basura")[0]["at_s"] == 10.0       # estimación inválida: se ignora
    assert layout_segments([{"headline": "A", "at_s": 10}], 30, 3)[0]["at_s"] == 10.0              # razón absurda (>2x): se ignora


def test_turns_reparte_por_texto_y_respeta_at_s():
    from playlist_api import layout_turns
    t = layout_turns([{"speaker": "Cap", "text": "a" * 10}, {"speaker": "nova", "text": "b" * 30, "emotion": "OH"},
                      {"speaker": "cap", "text": "c" * 10}], 50)
    assert [x["speaker"] for x in t] == ["cap", "nova", "cap"]
    assert [x["at_s"] for x in t] == [0.0, 10.0, 40.0]
    assert layout_turns([{"speaker": "cap", "at_s": 99}], 40)[0]["at_s"] == 40.0          # recortado a la duración
    assert t[1]["emotion"] == "oh" and "emotion" not in t[0]


def test_turns_descarta_basura_y_reescala():
    from playlist_api import layout_turns
    assert layout_turns("x", 10) == [] and layout_turns([5, {"speaker": ""}, {"speaker": "no valido!"}], 10) == []
    assert len(layout_turns([{"speaker": "cap", "w": 1}] * 200, 100)) == 60
    t = layout_turns([{"speaker": "cap", "at_s": 0}, {"speaker": "nova", "at_s": 10, "emotion": "x y"}], 22, 20)
    assert [x["at_s"] for x in t] == [0.0, 11.0] and "emotion" not in t[1]


def test_status_publica_turns_junto_al_faldon():
    from playout import chyron_of
    out = chyron_of({"metadata": {"chyron": {"headline": "h"}, "turns": [{"speaker": "cap", "at_s": 0.0}]}})
    assert out == {"headline": "h", "turns": [{"speaker": "cap", "at_s": 0.0}]}
    assert chyron_of({"metadata": {"turns": []}}) is None


def test_status_corre_los_tiempos_por_el_silencio_previo():
    from playout import chyron_of
    item = {"metadata": {"chyron": {"headline": "h"}, "segments": [{"headline": "A", "at_s": 1.0}],
                         "turns": [{"speaker": "cap", "at_s": 0.0}, {"speaker": "nova", "at_s": 4.5}]}}
    out = chyron_of(item, 0.4)
    assert [t["at_s"] for t in out["turns"]] == [0.4, 4.9] and out["segments"][0]["at_s"] == 1.4
    assert item["metadata"]["turns"][0]["at_s"] == 0.0                      # no muta el original
    assert chyron_of(item)["turns"][1]["at_s"] == 4.5
