"""JARVIS bundled plugin — offline unit converter (length, mass, volume,
temperature, speed, area, data, time)."""
import re

PLUGIN = {
    "name": "unit_converter",
    "version": "1.0.0",
    "description": (
        "Convert a value between units offline: length (km, miles, feet, inches, cm), mass (kg, lb, g, oz), "
        "volume (l, ml, gallon, cup), temperature (C, F, K), speed (km/h, mph, m/s, knots), area "
        "(sq ft, sq m, acre, hectare, cent), data (KB, MB, GB, TB), time (s, min, h, day, week)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "value": {"type": "NUMBER", "description": "The amount to convert"},
            "from_unit": {"type": "STRING", "description": "Unit to convert from, e.g. 'km'"},
            "to_unit": {"type": "STRING", "description": "Unit to convert to, e.g. 'miles'"},
        },
        "required": ["value", "from_unit", "to_unit"],
    },
    "risk": "safe",
    "permissions": [],
    "capabilities": ["convert", "units", "unit conversion", "length", "weight", "temperature", "measurement"],
}

_U = {
    "length": {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "mi": 1609.344, "mile": 1609.344, "yd": 0.9144,
               "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "in": 0.0254, "inch": 0.0254, "nmi": 1852},
    "mass": {"kg": 1, "g": 0.001, "mg": 1e-6, "lb": 0.45359237, "pound": 0.45359237, "oz": 0.028349523125,
             "tonne": 1000, "ton": 1000, "st": 6.35029318},
    "volume": {"l": 1, "litre": 1, "liter": 1, "ml": 0.001, "gal": 3.785411784, "gallon": 3.785411784,
               "cup": 0.2365882365, "floz": 0.0295735296, "m3": 1000},
    "speed": {"m/s": 1, "km/h": 1 / 3.6, "kmh": 1 / 3.6, "kph": 1 / 3.6, "mph": 0.44704, "knot": 0.514444},
    "area": {"m2": 1, "sqm": 1, "sqft": 0.09290304, "ft2": 0.09290304, "acre": 4046.8564224,
             "hectare": 10000, "ha": 10000, "km2": 1e6, "cent": 40.468564224},
    "data": {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4},
    "time": {"s": 1, "sec": 1, "min": 60, "h": 3600, "hr": 3600, "hour": 3600, "day": 86400, "week": 604800},
}
_ALIAS = {"kilometer": "km", "kilometre": "km", "meter": "m", "metre": "m", "centimeter": "cm", "millimeter": "mm",
          "miles": "mi", "yards": "yd", "inches": "in", "kilogram": "kg", "kilo": "kg", "kilos": "kg",
          "gram": "g", "grams": "g", "pounds": "lb", "lbs": "lb", "ounce": "oz", "ounces": "oz", "liters": "l",
          "litres": "l", "gallons": "gal", "cups": "cup", "square feet": "sqft", "sq ft": "sqft",
          "square meter": "m2", "sq m": "m2", "acres": "acre", "hectares": "ha", "cents": "cent",
          "knots": "knot", "hours": "hour", "minutes": "min", "seconds": "s", "days": "day", "weeks": "week",
          "celsius": "c", "fahrenheit": "f", "kelvin": "k", "°c": "c", "°f": "f"}


def _norm(u):
    u = re.sub(r"\s+", " ", str(u or "").strip().lower()).rstrip(".")
    u = _ALIAS.get(u, u)
    if u.endswith("s") and u[:-1] in {k for d in _U.values() for k in d}:
        u = u[:-1]
    return u


def convert(v, a, b):
    a, b = _norm(a), _norm(b)
    if {a, b} <= {"c", "f", "k"}:
        c = v if a == "c" else (v - 32) * 5 / 9 if a == "f" else v - 273.15
        return c if b == "c" else c * 9 / 5 + 32 if b == "f" else c + 273.15
    for table in _U.values():
        if a in table and b in table:
            return v * table[a] / table[b]
    raise ValueError(f"can't convert {a} to {b}")


def run(parameters, player=None, session_memory=None):
    try:
        v = float(parameters.get("value"))
        r = convert(v, parameters.get("from_unit"), parameters.get("to_unit"))
        return f"{v:g} {parameters.get('from_unit')} = {float(f'{r:.6g}'):g} {parameters.get('to_unit')}"
    except Exception as e:
        return f"I couldn't convert that ({e})."


def verify(parameters, result_text):
    return ("=" in result_text, "converted" if "=" in result_text else "conversion failed")
