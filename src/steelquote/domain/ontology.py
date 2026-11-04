"""Steel ontology: grade aliases, units, gauges, forms, and line normalization."""
import json
import math
import re
from functools import cache
from fractions import Fraction
from pathlib import Path

import pint

from steelquote.domain.schemas import ExtractedLine, Form, NormLine

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
PLATE_MIN_IN = 0.1875
FEET_MAX = 20  # "4x8" with no unit marks reads as feet only when both sides are this small

_ureg = pint.UnitRegistry()
_UNITS = {"mm": "mm", "cm": "cm", "m": "m", "in": "inch", "inch": "inch", "inches": "inch",
          '"': "inch", "ft": "foot", "feet": "foot", "foot": "foot", "'": "foot"}
_NUM = r"\d+[\s-]\d+/\d+|\d+/\d+|\d*\.\d+|\d+"
_UNIT = r"""inches|inch|feet|foot|mm|cm|in|ft|m|"|'"""
_LENGTH = re.compile(rf"^({_NUM})\s*({_UNIT})?$")
_GAUGE = re.compile(r"^#?(\d{1,2})\s*(?:ga|gauge|gage|g)\.?$")
_GAUGE_IN_TEXT = re.compile(r"\b(\d{1,2})\s*(?:ga|gauge|gage)\b")
_FRACTION_IN_TEXT = re.compile(r"(?<![\d/])(\d{1,2}/\d{1,2})(?![\d/])")
_DIMS = re.compile(rf"^({_NUM})\s*({_UNIT})?\s*(?:x|×|\*|by)\s*({_NUM})\s*({_UNIT})?$")
_NOISE = re.compile(r"\b(astm|aisi|sae|uns|type|grade|gr)\b")

_FORMS: dict[str, Form] = {
    "plt": "plate", "plate": "plate", "pl": "plate", "plates": "plate",
    "sht": "sheet", "sheet": "sheet", "sheets": "sheet",
    "flat": "flat_bar", "fb": "flat_bar", "flatbar": "flat_bar", "flats": "flat_bar",
    "round": "round_bar", "rd": "round_bar", "rod": "round_bar", "roundbar": "round_bar", "rnd": "round_bar",
}


@cache
def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _NOISE.sub("", text.lower()))


@cache
def _aliases() -> dict[str, str]:
    out = {}
    for grade, info in _load("grades.json")["grades"].items():
        for alias in (grade, *info["aliases"]):
            out[_key(alias)] = grade
    return out


def canonical_grade(text: str) -> str | None:
    key = _key(text)
    return _aliases().get(key) or _assumed(text)


def _assumed(text: str) -> str | None:
    return _load("grades.json")["assumed_aliases"].get(" ".join(text.lower().split()))


def grade_info(grade: str) -> dict:
    info = _load("grades.json")["grades"][grade]
    return {"uns": info["uns"], "family": info["family"], "density": info["density"]}


def _fraction(text: str) -> Fraction:
    return sum((Fraction(part) for part in re.split(r"[\s-]+", text)), Fraction(0))


def _gauge_in(n: int, family: str | None) -> tuple[float | None, str | None]:
    if family is None:
        return None, "gauge needs grade"
    table = _load("gauges.json")
    thk = table["tables"][table["family_table"][family]].get(str(n))
    return (thk, None) if thk else (None, f"unknown gauge: {n}")


def parse_length(text: str, family: str | None = None) -> tuple[float | None, str | None]:
    s = text.strip().lower()
    if m := _GAUGE.match(s):
        return _gauge_in(int(m[1]), family)
    if not (m := _LENGTH.match(s)):
        return None, f"unparseable length: {text!r}"
    qty = _ureg.Quantity(float(_fraction(m[1])), _UNITS[m[2] or "in"])
    return round(qty.to("inch").magnitude, 4), None


def parse_dims(text: str) -> tuple[float | None, float | None]:
    if not (m := _DIMS.match(text.strip().lower())):
        return None, None
    a, ua, b, ub = m.groups()
    unit = ua or ub
    if unit is None and max(float(_fraction(a)), float(_fraction(b))) <= FEET_MAX:
        unit = "ft"
    w, _ = parse_length(f"{a}{unit or 'in'}")
    l, _ = parse_length(f"{b}{ub or unit or 'in'}")
    return w, l


def normalize_form(text: str) -> Form | None:
    if form := _FORMS.get(re.sub(r"[^a-z]", "", text.lower())):
        return form
    # "rd bar", "sht metal": the generic word is noise, so try the remaining words one by one
    return next((_FORMS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in _FORMS), None)


def weight_lb(form: Form, grade: str, thk_in: float, w_in: float, l_in: float, qty: int = 1) -> float:
    density = grade_info(grade)["density"]
    volume = math.pi / 4 * thk_in**2 * l_in if form == "round_bar" else thk_in * w_in * l_in
    return round(volume * density * qty, 2)


def _dims(width: str | None, length: str | None, family: str | None, issues: list[str],
          label: str = "") -> tuple[float | None, float | None]:
    if width and length and not label and None not in (pair := parse_dims(f"{width} x {length}")):
        return pair
    if width and not length and None not in (pair := parse_dims(width)):
        return pair
    return tuple(_one(value, family, issues, f"{label}{name}") for value, name in
                 ((width, "width"), (length, "length")))


def _one(text: str | None, family: str | None, issues: list[str], name: str) -> float | None:
    if not text:
        return None
    value, issue = parse_length(text, family)
    if issue:
        issues.append(f"{name}: {issue}" if "needs grade" not in issue else issue)
    return value


def _ambiguity(line: ExtractedLine, family: str | None) -> str | None:
    text = f"{line.thickness or ''} {line.raw_text}".lower()
    gauge, frac = _GAUGE_IN_TEXT.search(text), _FRACTION_IN_TEXT.search(text)
    if not (gauge and frac):
        return None
    f_in, _ = parse_length(frac[1])
    g_in, _ = _gauge_in(int(gauge[1]), family)
    g_part = f"{g_in:.4f}" if g_in else "needs grade"
    return f"gauge vs fraction ambiguity: {frac[1]} ({f_in:.4f}) vs {gauge[1]}ga ({g_part})"


def _grade_fields(line: ExtractedLine, issues: list[str]) -> tuple[str | None, dict]:
    if not line.grade:
        issues.append("missing grade")
        return None, {}
    grade = canonical_grade(line.grade)
    if grade is None:
        issues.append(f"unknown grade: {line.grade}")
        return None, {}
    if _assumed(line.grade):
        issues.append(f"grade assumed {grade} from {line.grade!r}")
    return grade, grade_info(grade)


def _form_fields(line: ExtractedLine, thk: float | None, issues: list[str]) -> Form | None:
    form = normalize_form(line.form) if line.form else None
    if form or thk is None:
        if form is None:
            issues.append("missing form")
        return form
    issues.append("unknown form, inferred from thickness" if line.form else "form inferred from thickness")
    return "plate" if thk >= PLATE_MIN_IN else "sheet"


def normalize_line(i: int, line: ExtractedLine) -> NormLine:
    issues: list[str] = []
    grade, info = _grade_fields(line, issues)
    family = info.get("family")
    thk = _one(line.thickness, family, issues, "thickness")
    if line.thickness is None:
        issues.append("missing thickness")
    if ambiguity := _ambiguity(line, family):
        issues.append(ambiguity)
    form = _form_fields(line, thk, issues)
    w, l = _dims(line.width, line.length, family, issues)
    cut_w, cut_l = _dims(line.cut_width, line.cut_length, family, issues, "cut ")
    if w is None and form != "round_bar" and not line.width:
        issues.append("missing width")
    if l is None and not line.length:
        issues.append("missing length")
    return NormLine(line_no=i, raw_text=line.raw_text, qty=line.qty, grade=grade, uns=info.get("uns"),
                    form=form, thk_in=thk, w_in=w, l_in=l, cut_w_in=cut_w, cut_l_in=cut_l, issues=issues)
