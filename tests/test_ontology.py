import pytest

from steelquote.domain.ontology import (
    canonical_grade, grade_info, normalize_form, normalize_line, parse_dims, parse_length, weight_lb,
    _aliases, _key,
)
from steelquote.domain.schemas import ExtractedLine


@pytest.mark.parametrize("text, expected", [
    ("304", "304"), ("S30400", "304"), ("18-8", "304"), ("1.4301", "304"), ("SS304", "304"),
    ("304 SS", "304"), (" ss 304 ", "304"), ("304l", "304L"), ("316L", "316L"), ("S31603", "316L"),
    ("1.4404", "316L"), ("A96061", "6061"), ("6061-T6", "6061"), ("A-36", "A36"), ("ASTM A36", "A36"),
    ("a572 gr 50", "A572-50"), ("AISI 4140", "4140"), ("duplex", "2205"), ("mild steel", "A36"),
    ("unobtainium", None), ("", None),
])
def test_canonical_grade(text, expected):
    assert canonical_grade(text) == expected


def test_aliases_do_not_collide():
    from steelquote.domain.ontology import _load
    seen: dict[str, str] = {}
    for grade, info in _load("grades.json")["grades"].items():
        for alias in (grade, *info["aliases"]):
            assert seen.setdefault(_key(alias), grade) == grade, alias


def test_grade_info():
    assert grade_info("316L") == {"uns": "S31603", "family": "stainless", "density": 0.289}
    assert grade_info("6061")["family"] == "aluminum"


@pytest.mark.parametrize("text, expected", [
    ("1/4", 0.25), ("3/16", 0.1875), (".250", 0.25), ('0.25"', 0.25), ("1 1/2", 1.5), ("6mm", 0.2362),
    ("4'", 48.0), ("2 ft", 24.0), ("25.4 mm", 1.0),
])
def test_parse_length(text, expected):
    assert parse_length(text) == (pytest.approx(expected, abs=1e-4), None)


@pytest.mark.parametrize("text, family, expected", [
    ("7ga", "carbon", 0.1793), ("7 gauge", "carbon", 0.1793), ("7ga", "aluminum", 0.1443),
    ("7ga", "stainless", 0.1875), ("14 ga", "alloy", 0.0747),
])
def test_gauge_depends_on_material(text, family, expected):
    assert parse_length(text, family) == (expected, None)


def test_gauge_needs_grade():
    assert parse_length("7ga") == (None, "gauge needs grade")


def test_unparseable_length():
    value, issue = parse_length("thick")
    assert value is None and "unparseable" in issue


@pytest.mark.parametrize("text, expected", [
    ("4x8", (48.0, 96.0)), ("4 X 8", (48.0, 96.0)), ("48 x 96", (48.0, 96.0)), ('48"x96"', (48.0, 96.0)),
    ("5x10", (60.0, 120.0)), ("1219x2438mm", (47.9921, 95.9843)), ("nonsense", (None, None)),
])
def test_parse_dims(text, expected):
    assert parse_dims(text) == pytest.approx(expected, abs=1e-4) if expected[0] else expected


@pytest.mark.parametrize("text, expected", [
    ("plt", "plate"), ("PL", "plate"), ("Sheet", "sheet"), ("sht", "sheet"), ("flat bar", "flat_bar"),
    ("FB", "flat_bar"), ("rd", "round_bar"), ("rod", "round_bar"), ("rd bar", "round_bar"), ("coil", None),
])
def test_normalize_form(text, expected):
    assert normalize_form(text) == expected


def test_weight_plate_and_round():
    assert weight_lb("plate", "304", 0.25, 48, 96) == pytest.approx(332.93, abs=0.01)
    assert weight_lb("plate", "304", 0.25, 48, 96, qty=3) == pytest.approx(998.78, abs=0.02)
    assert weight_lb("round_bar", "1018", 2.0, 0, 12) == pytest.approx(3.14159 * 12 * 0.2836, abs=0.01)


def test_normalize_hero_line():
    line = ExtractedLine(raw_text="2 pcs 304 1/4 plt 4x8", qty=2, grade="SS304", form="plt",
                         thickness="1/4", width="4x8")
    n = normalize_line(1, line)
    assert (n.grade, n.uns, n.form, n.thk_in, n.w_in, n.l_in) == ("304", "S30400", "plate", 0.25, 48.0, 96.0)
    assert n.issues == []


def test_normalize_infers_form_and_flags_gaps():
    n = normalize_line(2, ExtractedLine(raw_text="x", qty=1, grade="A36", thickness="3/8"))
    assert n.form == "plate"
    assert n.issues == ["form inferred from thickness", "missing width", "missing length"]
    n = normalize_line(3, ExtractedLine(raw_text="x", grade="304", thickness="10ga", width="48", length="96"))
    assert n.form == "sheet" and n.thk_in == 0.1406


def test_normalize_missing_and_gauge_without_grade():
    n = normalize_line(1, ExtractedLine(raw_text="7ga sheet", form="sheet", thickness="7ga", width="48", length="96"))
    assert n.thk_in is None and "missing grade" in n.issues and "gauge needs grade" in n.issues
    n = normalize_line(2, ExtractedLine(raw_text="304 plate", grade="304", form="plate"))
    assert "missing thickness" in n.issues


def test_gauge_vs_fraction_ambiguity():
    line = ExtractedLine(raw_text="A36 3/16 (7ga) sheet 48x96", grade="A36", thickness="3/16")
    n = normalize_line(1, line)
    assert "gauge vs fraction ambiguity: 3/16 (0.1875) vs 7ga (0.1793)" in n.issues


def test_mild_steel_is_flagged_as_assumption():
    n = normalize_line(1, ExtractedLine(raw_text="x", grade="mild steel", thickness="1/4", form="plate",
                                        width="48", length="96"))
    assert n.grade == "A36" and n.issues == ["grade assumed A36 from 'mild steel'"]


def test_cut_dims():
    n = normalize_line(1, ExtractedLine(raw_text="x", grade="304", form="plate", thickness="1/2", width="48",
                                        length="96", cut_width="12", cut_length="18"))
    assert (n.cut_w_in, n.cut_l_in) == (12.0, 18.0)  # cut sizes are inches, never the 4x8 feet shorthand
