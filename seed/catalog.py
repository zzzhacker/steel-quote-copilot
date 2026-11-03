"""Pure data generation for the demo catalog: products, lots, partners. No I/O, fully seeded."""
import math
import random
from dataclasses import dataclass
from fractions import Fraction

from faker import Faker

SEED = 42
QUOTA_PER_GROUP = 7  # SKUs per (grade, form); 36 groups -> ~250 products

# canonical grade -> (UNS, family prefix, forms stocked)
_ALL = ("plate", "sheet", "flat_bar", "round_bar")
GRADES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "304": ("S30400", "SS", _ALL), "304L": ("S30403", "SS", _ALL),
    "316": ("S31600", "SS", _ALL), "316L": ("S31603", "SS", _ALL),
    "430": ("S43000", "SS", ("sheet",)), "2205": ("S32205", "SS", ("plate",)),
    "A36": ("K02600", "CS", _ALL), "A572-50": ("-", "CS", ("plate", "flat_bar")),
    "1018": ("G10180", "CS", ("round_bar", "flat_bar")), "4140": ("G41400", "CS", ("round_bar", "flat_bar")),
    "6061": ("A96061", "AL", _ALL), "5052": ("A95052", "AL", _ALL),
}
FAMILY_NAME = {"SS": "Stainless", "CS": "Carbon Steel", "AL": "Aluminum"}
FORM_CODE = {"plate": "PL", "sheet": "SH", "flat_bar": "FB", "round_bar": "RB"}
FORM_NAME = {"plate": "Plate", "sheet": "Sheet", "flat_bar": "Flat Bar", "round_bar": "Round Bar"}
DENSITY = {"SS": 0.289, "CS": 0.2836, "AL": 0.0975}  # lb/in^3
PRICE_PER_LB = {"SS": 2.80, "CS": 0.75, "AL": 2.60}  # placeholder; real pricing lives in the engine

GAUGE_IN = {7: 0.1793, 10: 0.1345, 11: 0.1196, 12: 0.1046, 14: 0.0747, 16: 0.0598, 18: 0.0478, 20: 0.0359}
_SHEET_SIZES = [(48, 96), (48, 120), (60, 120)]
_DIM_OPTIONS = {
    "plate": [(t, w, l) for t in (0.1875, 0.25, 0.3125, 0.375, 0.5, 0.625, 0.75, 1.0, 1.25, 1.5, 2.0)
              for w, l in _SHEET_SIZES],
    "sheet": [(t, w, l) for t in GAUGE_IN.values() for w, l in _SHEET_SIZES],
    "flat_bar": [(t, w, l) for t in (0.25, 0.375, 0.5, 0.75, 1.0) for w in (1, 1.5, 2, 3, 4, 6) for l in (144, 240)],
    "round_bar": [(d, d, l) for d in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0) for l in (144, 240)],  # thk = w = dia
}
# (grade, form, thk, w, l) that must exist: demo cases
MUST_STOCK = [
    ("304", "plate", 0.25, 48, 96), ("304", "plate", 0.25, 48, 120),
    ("316L", "plate", 0.375, 48, 96), ("A36", "sheet", 0.1793, 48, 96), ("1018", "round_bar", 2.0, 2.0, 144),
]

MILLS = {"SS": ["NAS", "Outokumpu", "Cleveland-Cliffs"], "CS": ["Nucor", "Steel Dynamics", "Cleveland-Cliffs"],
         "AL": ["Novelis", "Constellium"]}
REMNANT_SIZES = [(24, 48), (30, 60), (36, 40), (36, 72), (24, 36), (40, 48)]
RACKS = [f"Rack {r}{n}" for r in "ABCD" for n in range(1, 7)]

# deterministic demo scenarios: [(w, l, qty, is_remnant)]
FIXED_LOTS = {
    "SS304-PL-0.250-48x96": [(48, 96, 8, False), (48, 96, 4, False), (24, 48, 2, True), (36, 72, 1, True)],
    "SS316L-PL-0.375-48x96": [(48, 96, 1, False)],  # shortage case: one piece total
    "CSA36-SH-0.1793-48x96": [(48, 96, 6, False), (48, 96, 3, False)],
    "CS1018-RB-2.000-2x144": [(2.0, 144, 7, False), (2.0, 144, 4, False)],
}

# manual (x_) fields created over the API: model -> [(name, label, ttype)]
CUSTOM_FIELDS = {
    "product.template": [("x_grade", "Grade", "char"), ("x_uns", "UNS", "char"), ("x_form", "Form", "char"),
                         ("x_thk_in", "Thickness (in)", "float"), ("x_w_in", "Width (in)", "float"),
                         ("x_l_in", "Length (in)", "float")],
    "stock.lot": [("x_mill", "Mill", "char"), ("x_is_remnant", "Remnant", "boolean"),
                  ("x_w_in", "Width (in)", "float"), ("x_l_in", "Length (in)", "float")],
    "res.partner": [("x_tier", "Customer Tier", "char")],
}

SERVICES = [("SVC-SAW", "Saw Cut", 4.00), ("SVC-SHEAR", "Shear Cut", 2.50), ("SVC-MTR", "MTR Cert", 15.00),
            ("SVC-FREIGHT", "Freight", 125.00), ("SVC-ALLOY", "Alloy Surcharge", 1.00)]


@dataclass(frozen=True)
class Product:
    default_code: str
    name: str
    grade: str
    uns: str
    family: str
    form: str
    thk_in: float
    w_in: float
    l_in: float
    weight_lb: float
    list_price: float


@dataclass(frozen=True)
class Lot:
    product_code: str
    name: str
    mill: str
    is_remnant: bool
    w_in: float
    l_in: float
    qty: int
    rack: str


@dataclass(frozen=True)
class Partner:
    ref: str
    name: str
    email: str
    city: str
    tier: str


def _inches(x: float) -> str:
    """0.375 -> '3/8', 1.25 -> '1 1/4', 48 -> '48'."""
    f = Fraction(x).limit_denominator(16)
    whole, rem = divmod(f.numerator, f.denominator)
    parts = ([str(whole)] if whole else []) + ([f"{rem}/{f.denominator}"] if rem else [])
    return " ".join(parts) or "0"


def _make_product(grade: str, form: str, thk: float, w: float, l: float) -> Product:
    uns, fam, _ = GRADES[grade]
    if form == "sheet":
        gauge = next(g for g, t in GAUGE_IN.items() if t == thk)
        thk_txt = f"{gauge}ga"
    else:
        thk_txt = _inches(thk)
    thk_code = f"{thk:.4f}".removesuffix("0")  # 0.2500 -> 0.250, 0.1875 stays
    size = f"{_inches(w)} dia x {_inches(l)}" if form == "round_bar" else f"{thk_txt} x {_inches(w)} x {_inches(l)}"
    area = math.pi / 4 * w * w if form == "round_bar" else thk * w
    weight = round(area * l * DENSITY[fam], 1)
    return Product(
        default_code=f"{fam}{grade}-{FORM_CODE[form]}-{thk_code}-{w:g}x{l:g}",
        name=f"{grade} {FAMILY_NAME[fam]} {FORM_NAME[form]} {size}",
        grade=grade, uns=uns, family=fam, form=form, thk_in=thk, w_in=float(w), l_in=float(l),
        weight_lb=weight, list_price=round(weight * PRICE_PER_LB[fam], 2),
    )


def products(seed: int = SEED) -> list[Product]:
    rng = random.Random(seed)
    must = set(MUST_STOCK)
    out = []
    for grade, (_, _, forms) in GRADES.items():
        for form in forms:
            options = _DIM_OPTIONS[form]
            fixed = [o for o in options if (grade, form, *o) in must]
            rest = [o for o in options if o not in fixed]
            picks = fixed + rng.sample(rest, min(len(rest), max(0, QUOTA_PER_GROUP - len(fixed))))
            out += [_make_product(grade, form, *o) for o in sorted(picks)]
    return out


def _heat(rng: random.Random, used: set[str]) -> str:
    while True:
        base = f"{rng.choice('HJKLMNPT')}{rng.randint(10000, 59999)}"
        if base not in used:
            used.add(base)
            return base


def _lot_plan(p: Product, rng: random.Random) -> list[tuple[float, float, int, bool]]:
    if p.default_code in FIXED_LOTS:
        return FIXED_LOTS[p.default_code]
    plan = [(p.w_in, p.l_in, rng.randint(1, 12), False) for _ in range(rng.choice([1, 2, 2, 3, 3, 4]))]
    if p.form in ("plate", "sheet") and rng.random() < 0.9:
        fits = [s for s in REMNANT_SIZES if s[0] <= p.w_in and s[1] <= p.l_in]
        w, l = rng.choice(fits)
        plan.append((w, l, rng.randint(1, 3), True))
    return plan


def lots(prods: list[Product], seed: int = SEED) -> list[Lot]:
    rng = random.Random(seed + 1)
    used: set[str] = set()
    out = []
    for p in prods:
        for w, l, qty, rem in _lot_plan(p, rng):
            heat = _heat(rng, used)
            name = f"{heat}-R{rng.randint(1, 4)}" if rem else f"{heat}-{rng.choice('ABCD')}"
            out.append(Lot(p.default_code, name, rng.choice(MILLS[p.family]), rem, float(w), float(l), qty,
                           rng.choice(RACKS)))
    return out


_SUFFIXES = ["Fabrication", "Machine Works", "Metalworks", "Welding & Fab", "Manufacturing", "Tool & Die",
             "Industries", "Precision Machining"]
_FIXED_PARTNERS = [("Kessler Fabrication", "purchasing@kesslerfab.com", "Dayton", "A"),
                   ("Brightline Machine Works", "orders@brightlinemw.com", "Rockford", "B")]


def partners(n: int = 20, seed: int = SEED) -> list[Partner]:
    fake, rng = Faker("en_US"), random.Random(seed + 2)
    Faker.seed(seed)
    rows = list(_FIXED_PARTNERS)
    names = {r[0] for r in rows}
    while len(rows) < n:
        name = f"{fake.last_name()} {rng.choice(_SUFFIXES)}"
        if name in names:
            continue
        names.add(name)
        domain = "".join(c for c in name.lower() if c.isalnum())[:18] + ".com"
        rows.append((name, f"purchasing@{domain}", fake.city(), rng.choices("ABC", weights=[3, 4, 3])[0]))
    return [Partner(f"C{i:03d}", *r) for i, r in enumerate(rows, 1)]
