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
