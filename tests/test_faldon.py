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
