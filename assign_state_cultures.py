#!/usr/bin/env python3
"""Assign TNO nationality IDs to LOTD states using geographic rules."""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import RBFInterpolator

from assign_infrastructure import (
    POPULATION_CSV,
    ROOT,
    STATE_DIR,
    load_state_names,
    parse_definition,
    parse_states,
    rasterize_provinces,
)
from assign_victory_points import (
    ANCHOR_SNAPSHOT,
    US_STATE_ABBRS,
    compute_province_centroids,
    fit_forward_rbf,
    load_or_create_anchor_snapshot,
    parse_known_vp_localisations,
)

TNO_MOD_ROOT = Path.home() / (
    "Library/Application Support/Steam/steamapps/workshop/content/394360/2438003901"
)
TNO_CULTURE_FILE = TNO_MOD_ROOT / "common/scripted_effects/TNO_Culture_scripted_effects.txt"
TNO_STATE_DIR = TNO_MOD_ROOT / "history/states"

OUTPUT_CSV = ROOT / "culture_assignments.csv"
TNO_REFERENCE_CSV = ROOT / "tno_culture_reference.csv"
EFFECT_FILE = ROOT / "common/scripted_effects/LOTD_Culture_scripted_effects.txt"
VP_LOCALISATION = ROOT / "localisation/english/TNO_victory_points_l_english.yml"
PROVINCES_BMP = ROOT / "map/provinces.bmp"
DEFINITION_CSV = ROOT / "map/definition.csv"

NA_NATIONALITY_IDS = (
    set(range(82, 110))
    | {337, 365, 366, 367}
    | set(range(558, 575))
    | set(range(695, 723))
    | {89, 572, 708}
)

CULTURE_LABELS: dict[int, str] = {
    82: "New Englander",
    83: "Dixie",
    84: "Dixie-African American",
    85: "Steel Belt",
    86: "Texan",
    87: "Frontier",
    88: "Californian",
    89: "Hawaiian",
    90: "Boricua",
    92: "Ontarian",
    93: "Quebecois",
    94: "Inuit",
    95: "Maritimer",
    96: "Antillean",
    97: "Guianan",
    98: "Dutch-Antillean",
    99: "Jamaican",
    100: "Cuban",
    101: "Haitian",
    102: "Quisqueyano",
    103: "Belizean",
    104: "Guatemalan",
    105: "Honduran",
    106: "Salvadorian",
    107: "Nicaraguan",
    108: "Costa Rican",
    109: "Panamanian",
    337: "Midwestern",
    365: "Western Canadian",
    366: "British Columbian",
    367: "Newfoundlander",
    558: "New York",
    559: "Mid-Atlantic",
    560: "Mid-Atlantic (Maryland)",
    561: "African-American Mid-Atlantic (DC)",
    562: "Mid-Atlantic / Steel Belt (Pennsylvania)",
    563: "Steel Belt (IL/MI)",
    564: "Missourian",
    565: "Appalachian",
    566: "Appalachian-African American",
    567: "Tidewater",
    568: "Floridian",
    569: "Louisianan",
    570: "Oklahoman",
    571: "Southern Frontier",
    572: "Alaskan",
    573: "Mormon",
    574: "Pacific Northwestern",
    695: "Bajan Mexican",
    696: "Bajan Mexican",
    697: "Norteno Mexican (Sonora)",
    698: "Norteno Mexican (Chihuahua)",
    699: "Norteno Mexican (Coahuila)",
    700: "Norteno Mexican (Nuevo Leon/Tamaulipas)",
    701: "Norteno Mexican (Sinaloa)",
    702: "Norteno Mexican (Durango/Zacatecas/Aguascalientes)",
    705: "Norteno Mexican (San Luis Potosi)",
    706: "Altiplano Mexican (Veracruz)",
    707: "Occidental Mexican (Nayarit/Jalisco/Colima)",
    709: "Occidental Mexican (Michoacan/Guanajuato/Queretaro)",
    715: "Altiplano Mexican (Morelos)",
    716: "Altiplano Mexican (Federal District)",
    717: "Altiplano Mexican (Puebla)",
    718: "Altiplano Mexican (Guerrero)",
    719: "Oaxacan Mexican",
    720: "Chiapan Mexican",
    721: "Sureno Mexican (Tabasco)",
    722: "Mayan Mexican (Yucatan)",
}

US_STATE_FULL_NAME_TO_ABBR: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

MEXICO_ABBR_DEFAULT: dict[str, int] = {
    "SO": 697,  # Sonora
    "CH": 698,  # Chihuahua
    "CO": 699,  # Coahuila
    "NL": 700,  # Nuevo Leon
    "TM": 700,  # Tamaulipas
    "TA": 700,
    "SI": 701,  # Sinaloa
    "DG": 702,  # Durango
    "ZA": 702,  # Zacatecas
    "SL": 705,  # San Luis Potosi
    "VC": 706,  # Veracruz
    "JA": 707,  # Jalisco
    "NA": 707,  # Nayarit
    "MI": 709,  # Michoacan
    "GT": 709,  # Guanajuato
    "QT": 709,  # Queretaro
    "MX": 716,  # Mexico state / CDMX region
    "DF": 716,
    "PU": 717,  # Puebla
    "GR": 718,  # Guerrero
    "OA": 719,  # Oaxaca
    "CP": 720,  # Chiapas
    "TB": 721,  # Tabasco
    "YU": 722,  # Yucatan
    "QR": 722,  # Quintana Roo
    "CM": 722,  # Campeche
    "HG": 716,  # Hidalgo (altiplano)
}

US_STATE_DEFAULT: dict[str, int] = {
    "CT": 82, "ME": 82, "MA": 82, "NH": 82, "RI": 82, "VT": 82,
    "NY": 82,
    "NJ": 559, "DE": 559,
    "PA": 562, "MD": 560, "DC": 561,
    "VA": 567, "NC": 567, "SC": 84,
    "GA": 83, "AL": 83, "MS": 84, "AR": 83,
    "FL": 568, "LA": 569,
    "TN": 566, "KY": 565, "WV": 565,
    "OH": 85, "IN": 85, "WI": 85,
    "MI": 563, "IL": 563,
    "MN": 337, "IA": 337, "SD": 337, "ND": 337, "NE": 337, "KS": 337,
    "MO": 564, "OK": 570, "TX": 86,
    "NM": 571, "AZ": 571, "CO": 571,
    "CA": 88, "NV": 87, "UT": 573, "WY": 87, "ID": 87, "MT": 87,
    "OR": 574, "WA": 574,
    "AK": 572, "HI": 89, "PR": 90,
}

CANADA_PROVINCE_DEFAULT: dict[str, int] = {
    "quebec": 93,
    "ontario": 92,
    "british columbia": 366,
    "alberta": 365,
    "saskatchewan": 365,
    "manitoba": 365,
    "nova scotia": 95,
    "new brunswick": 95,
    "prince edward island": 95,
    "newfoundland": 367,
    "labrador": 367,
    "yukon": 94,
    "northwest territories": 94,
    "nunavut": 94,
}

MEXICO_NAME_DEFAULT: list[tuple[tuple[str, ...], int]] = [
    (("baja california sur",), 696),
    (("baja california",), 695),
    (("sonora",), 697),
    (("chihuahua",), 698),
    (("coahuila",), 699),
    (("nuevo leon", "nuevo león", "monterrey", "tamaulipas", "matamoros", "laredos", "ciudad victoria"), 700),
    (("sinaloa", "los mochis", "mazatlan", "el carrizo"), 701),
    (("durango", "zacatecas", "aguascalientes", "fresnillo", "gomez palacio"), 702),
    (("san luis potosi", "san luis potosí", "ciudad valles", "axtla", "monctezuma"), 705),
    (("veracruz",), 706),
    (("jalisco", "nayarit", "colima", "puerto vallarta", "mezquitic", "colotlan"), 707),
    (("michoacan", "michoacán", "guanajuato", "queretaro", "querétaro", "morelia"), 709),
    (("morelos",), 715),
    (("mexico city", "tenochtitlan", "toluca", "ecatepec", "naucalpan", "tlalnepantla", "cuautitlan", "iztapalapa", "gustavo a. madero", "miguel hidalgo", "benito juarez", "cuauhtemoc", "venustiano carranza", "lopez mateos"), 716),
    (("puebla",), 717),
    (("guerrero",), 718),
    (("oaxaca",), 719),
    (("chiapas",), 720),
    (("tabasco",), 721),
    (("yucatan", "yucatán", "quintana roo", "campeche", "merida", "cancun"), 722),
    (("hidalgo, mx",), 716),
]

CARIBBEAN_PATTERNS: list[tuple[tuple[str, ...], int]] = [
    (("puerto rico", ", pr", "vieques", "culebra", "isla de mona", "ponce", "mayaguez", "arecibo", "guayama", "san juan"), 90),
    (("us virgin islands", "u.s. virgin islands"), 98),
    (("saint pierre", "saint-pierre", "miquelon"), 95),
    (("cuba", ", cu", "havana", "habana", "santiago de cuba", "guantanamo", "cienfuegos", "las tunas", "isla de la juventud"), 100),
    (("haiti", ", ht", "artibonite", "nord-est", "nord,"), 101),
    (("dominican", ", do", "santo domingo", "maria trinidad", "samana", "monte cristi", "puerto plata", "duarte", "el seybo", "la altagracia", "santiago, do", "hato mayor", "la vega", "dajabon", "la romana", "san pedro de macoris", "azua", "peravia", "barahona", "la estrelleta"), 102),
    (("jamaica", ", jm"), 99),
    (("bahamas", ", bs", "nassau", "freeport", "new providence", "eleuthera", "andros", "cat island", "san salvador, bs", "long island, bs", "great exuma", "crooked island", "mayaguana", "inagua"), 96),
    (("belize", ", bz"), 103),
    (("guatemala", ", gt"), 104),
    (("honduras", ", hn"), 105),
    (("el salvador", ", sv"), 106),
    (("nicaragua", ", ni"), 107),
    (("costa rica", ", cr"), 108),
    (("panama", ", pa"), 109),
    (("turks and caicos",), 96),
    (("british virgin", "bvi"), 98),
    (("saint-pierre", "miquelon"), 95),
]

NYC_BOROUGH_STATE_IDS = {774, 775, 776, 777}


@dataclass
class RegionBox:
    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float
    culture_id: int
    rule: str
    states: frozenset[str] | None = None

    def matches(self, lon: float, lat: float, state_abbr: str | None) -> bool:
        if not (self.lon_min <= lon <= self.lon_max and self.lat_min <= lat <= self.lat_max):
            return False
        if self.states is not None and state_abbr not in self.states:
            return False
        return True


US_REGION_BOXES: list[RegionBox] = [
    RegionBox(-77.15, -76.85, 38.79, 39.05, 561, "dc_box", frozenset({"DC"})),
    RegionBox(-74.6, -73.55, 40.45, 41.05, 558, "nyc_metro", frozenset({"NY"})),
    RegionBox(-75.6, -73.55, 39.8, 40.45, 559, "mid_atlantic_south", frozenset({"NJ", "DE"})),
    RegionBox(-80.0, -74.0, 39.0, 40.2, 562, "pennsylvania_south", frozenset({"PA"})),
    RegionBox(-80.8, -74.8, 40.2, 42.5, 562, "pennsylvania_north", frozenset({"PA"})),
    RegionBox(-91.0, -79.0, 29.5, 33.5, 84, "black_belt_core", frozenset({"MS", "SC"})),
    RegionBox(-88.8, -85.0, 31.5, 33.2, 84, "black_belt_al", frozenset({"AL"})),
    RegionBox(-85.2, -80.8, 30.5, 33.0, 84, "black_belt_ga", frozenset({"GA"})),
    RegionBox(-91.8, -88.0, 30.0, 32.8, 84, "black_belt_la", frozenset({"LA"})),
    RegionBox(-83.8, -78.0, 36.5, 40.8, 565, "appalachian_core", frozenset({"WV"})),
    RegionBox(-85.8, -81.8, 36.0, 39.8, 565, "appalachian_ky", frozenset({"KY"})),
    RegionBox(-84.8, -78.0, 35.0, 37.8, 565, "appalachian_mtn", frozenset({"VA", "NC", "TN"})),
    RegionBox(-77.8, -75.0, 35.5, 37.8, 567, "tidewater", frozenset({"VA", "NC"})),
    RegionBox(-73.8, -66.9, 41.0, 47.6, 82, "new_england_lat", None),
    RegionBox(-124.8, -116.8, 42.0, 49.1, 574, "pacific_nw", frozenset({"OR", "WA"})),
    RegionBox(-114.2, -109.0, 36.8, 42.2, 573, "mormon_corridor", frozenset({"UT", "ID", "WY"})),
    RegionBox(-109.2, -102.0, 31.2, 37.2, 571, "southern_frontier", frozenset({"NM", "AZ", "CO"})),
    RegionBox(-124.6, -114.0, 32.4, 42.2, 88, "california", frozenset({"CA"})),
    RegionBox(-120.2, -104.0, 25.6, 36.8, 86, "texas", frozenset({"TX"})),
]


def parse_tno_state_names(state_dir: Path) -> dict[int, str]:
    names: dict[int, str] = {}
    for path in state_dir.glob("*.txt"):
        match = re.match(r"^(\d+)-(.+)\.txt$", path.name)
        if not match:
            continue
        names[int(match.group(1))] = match.group(2).replace("_", " ")
    return names


def parse_tno_culture_assignments(culture_file: Path) -> list[dict]:
    text = culture_file.read_text(encoding="utf-8")
    current_comment = "Unknown"
    rows: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            current_comment = stripped.lstrip("#").strip()
            continue
        match = re.match(r"set_variable\s*=\s*\{\s*(\d+)\.nationality\s*=\s*(\d+)\s*\}", stripped)
        if not match:
            continue
        state_id = int(match.group(1))
        nationality = int(match.group(2))
        if nationality not in NA_NATIONALITY_IDS:
            continue
        rows.append(
            {
                "tno_state_id": state_id,
                "nationality_id": nationality,
                "culture_block": current_comment,
            }
        )
    return rows


def export_tno_reference(rows: list[dict], state_names: dict[int, str], output: Path) -> None:
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["tno_state_id", "tno_state_name", "nationality_id", "culture_label", "culture_block"],
        )
        writer.writeheader()
        for row in sorted(rows, key=lambda item: (item["nationality_id"], item["tno_state_id"])):
            nationality = row["nationality_id"]
            writer.writerow(
                {
                    "tno_state_id": row["tno_state_id"],
                    "tno_state_name": state_names.get(row["tno_state_id"], ""),
                    "nationality_id": nationality,
                    "culture_label": CULTURE_LABELS.get(nationality, f"var_nationality.{nationality}"),
                    "culture_block": row["culture_block"],
                }
            )


def build_inverse_rbf(
    pairs: list[tuple[float, float, float, float, int, str]],
) -> tuple[RBFInterpolator, RBFInterpolator]:
    pixels = np.array([[x, y] for _, _, x, y, _, _ in pairs], dtype=float)
    lons = np.array([lon for lon, _, _, _, _, _ in pairs], dtype=float)
    lats = np.array([lat for _, lat, _, _, _, _ in pairs], dtype=float)
    rbf_lon = RBFInterpolator(pixels, lons, kernel="thin_plate_spline", smoothing=0.5)
    rbf_lat = RBFInterpolator(pixels, lats, kernel="thin_plate_spline", smoothing=0.5)
    return rbf_lon, rbf_lat


def state_centroid_latlon(
    state: dict,
    centroids: dict[int, tuple[float, float]],
    rbf_lon: RBFInterpolator,
    rbf_lat: RBFInterpolator,
) -> tuple[float | None, float | None]:
    xs: list[float] = []
    ys: list[float] = []
    for province_id in state["provinces"]:
        centroid = centroids.get(province_id)
        if centroid:
            xs.append(centroid[0])
            ys.append(centroid[1])
    if not xs:
        return None, None
    px = float(np.mean(xs))
    py = float(np.mean(ys))
    lon = float(rbf_lon([[px, py]])[0])
    lat = float(rbf_lat([[px, py]])[0])
    return lon, lat


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def parse_trailing_abbr(name: str) -> str | None:
    normalized = normalize_name(name)
    if normalized in US_STATE_FULL_NAME_TO_ABBR:
        return US_STATE_FULL_NAME_TO_ABBR[normalized]
    match = re.search(r",\s*([a-z]{2,3})\s*$", normalized)
    if match:
        token = match.group(1).upper()
        if token == "NYC":
            return "NYC"
        return token
    match = re.search(r",\s*([a-z][a-z\s]+)$", normalized)
    if match:
        full = match.group(1).strip()
        if full in US_STATE_FULL_NAME_TO_ABBR:
            return US_STATE_FULL_NAME_TO_ABBR[full]
    return None


def disambiguate_abbr(abbr: str, name: str, lat: float | None) -> str:
    normalized = normalize_name(name)
    if abbr == "BC" and lat is not None and lat < 42 and "british columbia" not in normalized:
        return "MX_BC"
    if abbr == "NL" and lat is not None and lat < 35 and "newfoundland" not in normalized:
        return "MX_NL"
    if abbr == "MI" and lat is not None and lat < 40 and "michigan" not in normalized:
        return "MX_MI"
    if abbr == "CO" and lat is not None and lat < 37 and "colorado" not in normalized:
        return "MX_CO"
    if abbr == "MO" and lat is not None and lat < 24 and "missouri" not in normalized:
        return "MX_MO"
    if abbr == "DG" and lat is not None and lat < 37 and "delaware" not in normalized and "durango" not in normalized:
        return "MX_DG"
    if abbr in MEXICO_ABBR_DEFAULT and lat is not None and lat < 37:
        return f"MX_{abbr}"
    if abbr in US_STATE_ABBRS and lat is not None and lat < 24:
        return f"CARIB_{abbr}"
    return abbr


def match_name_patterns(name: str, patterns: list[tuple[tuple[str, ...], int]]) -> int | None:
    normalized = normalize_name(name)
    for needles, culture_id in patterns:
        if any(needle in normalized for needle in needles):
            return culture_id
    return None


def match_canada_province(name: str) -> int | None:
    normalized = normalize_name(name)
    if ", qc" in normalized or normalized.endswith(" qc"):
        return 93
    if ", on" in normalized or normalized.endswith(" on"):
        return 92
    if ", bc" in normalized or "british columbia" in normalized:
        return 366
    if any(token in normalized for token in (", ab", "alberta")):
        return 365
    if any(token in normalized for token in (", sk", "saskatchewan", "saskatchwan")):
        return 365
    if any(token in normalized for token in (", mb", "manitoba")):
        return 365
    if any(token in normalized for token in (", ns", "nova scotia")):
        return 95
    if any(token in normalized for token in (", nb", "new brunswick")):
        return 95
    if any(token in normalized for token in ("prince edward", ", pe", ", pei")):
        return 95
    if "newfoundland" in normalized or "labrador" in normalized:
        return 367
    if any(token in normalized for token in ("nunavut", ", nu")):
        return 94
    if any(token in normalized for token in ("northwest territories", ", nt")):
        return 94
    if any(token in normalized for token in ("yukon", ", yt")):
        return 94
    for province, culture_id in CANADA_PROVINCE_DEFAULT.items():
        if normalized == province or normalized.startswith(province + ","):
            return culture_id
    return None


def assign_culture(
    state_id: int,
    name: str,
    lon: float | None,
    lat: float | None,
    filename: str,
) -> tuple[int, str]:
    normalized = normalize_name(name)
    if state_id == 26 or normalized == "world border":
        return 0, "world_border"

    if state_id in NYC_BOROUGH_STATE_IDS or "bronx" in filename.lower() or "brooklyn" in filename.lower() or "queens" in filename.lower() or "staten island" in filename.lower():
        return 558, "nyc_borough_file"

    if normalized in US_STATE_FULL_NAME_TO_ABBR:
        abbr = US_STATE_FULL_NAME_TO_ABBR[normalized]
        return US_STATE_DEFAULT[abbr], "us_full_state_name"

    if normalized == "district of columbia":
        return 561, "dc_name"

    if ", nyc" in normalized or normalized.startswith("manhattan"):
        return 558, "nyc_name"

    if normalized == "hidalgo" and lat is not None and lat < 30:
        return 716, "mexico_hidalgo_state"

    caribbean = match_name_patterns(name, CARIBBEAN_PATTERNS)
    if caribbean is not None:
        return caribbean, "caribbean_name"

    canada = match_canada_province(name)
    if canada is not None:
        return canada, "canada_name"

    mexico = match_name_patterns(name, MEXICO_NAME_DEFAULT)
    if mexico is not None:
        return mexico, "mexico_name"

    abbr = parse_trailing_abbr(name)
    if abbr:
        if abbr == "NYC":
            return 558, "nyc_abbr"
        abbr = disambiguate_abbr(abbr, name, lat)
        if abbr.startswith("MX_"):
            mx_abbr = abbr[3:]
            return MEXICO_ABBR_DEFAULT.get(mx_abbr, match_name_patterns(name, MEXICO_NAME_DEFAULT) or 697), "mexico_abbr"
        if abbr.startswith("CARIB_"):
            return match_name_patterns(name, CARIBBEAN_PATTERNS) or 96, "caribbean_abbr"

    if lon is not None and lat is not None:
        effective_abbr = abbr if abbr and not abbr.startswith(("MX_", "CARIB_")) else None
        for box in US_REGION_BOXES:
            if box.matches(lon, lat, effective_abbr):
                return box.culture_id, box.rule

    if abbr and abbr in US_STATE_DEFAULT:
        return US_STATE_DEFAULT[abbr], "us_state_default"

    if lon is not None and lat is not None:
        if 24 <= lat <= 32 and -85 <= lon <= -75:
            return 568, "florida_lat"
        if 42 <= lat <= 45 and -80 <= lon <= -76:
            return 82, "western_ny_lat"
        if lat >= 49 and lon <= -95:
            return 94, "northern_canada_lat"
        if 14 <= lat <= 33 and lon <= -105:
            return 697, "mexico_lat_fallback"

    return 0, "unassigned"


def write_culture_effect(assignments: dict[int, tuple[int, str]]) -> None:
    grouped: dict[int, list[int]] = defaultdict(list)
    for state_id, (culture_id, _) in sorted(assignments.items()):
        if culture_id <= 0:
            continue
        grouped[culture_id].append(state_id)

    lines = [
        "############################",
        "## LOTD culture setup — generated by assign_state_cultures.py",
        "## Do not edit by hand; re-run the script instead.",
        "############################",
        "",
        "LOTD_setup_cultures = {",
    ]
    for culture_id in sorted(grouped):
        label = CULTURE_LABELS.get(culture_id, f"Culture {culture_id}")
        lines.append(f"\t# {label} ({culture_id})")
        for state_id in grouped[culture_id]:
            lines.append(f"\tset_variable = {{ {state_id}.nationality = {culture_id} }}")
        lines.append("")
    lines.append("}")
    lines.append("")
    EFFECT_FILE.write_text("\n".join(lines), encoding="utf-8")


def write_assignments_csv(rows: list[dict]) -> None:
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["lotd_state_id", "name", "lat", "lon", "nationality_id", "culture_name", "rule_used"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign TNO cultures to LOTD states.")
    parser.add_argument("--tno-root", type=Path, default=TNO_MOD_ROOT)
    args = parser.parse_args()

    culture_file = args.tno_root / "common/scripted_effects/TNO_Culture_scripted_effects.txt"
    state_dir = args.tno_root / "history/states"
    if not culture_file.exists():
        raise SystemExit(f"TNO culture file not found: {culture_file}")

    tno_rows = parse_tno_culture_assignments(culture_file)
    tno_state_names = parse_tno_state_names(state_dir)
    export_tno_reference(tno_rows, tno_state_names, TNO_REFERENCE_CSV)

    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    centroids = compute_province_centroids(province_raster)
    known_vps = parse_known_vp_localisations(VP_LOCALISATION)
    pairs = load_or_create_anchor_snapshot(known_vps, centroids)
    rbf_lon, rbf_lat = build_inverse_rbf(pairs)

    states = parse_states(STATE_DIR)
    state_names = load_state_names(POPULATION_CSV)

    assignment_rows: list[dict] = []
    assignments: dict[int, tuple[int, str]] = {}
    for state_id in sorted(states):
        state = states[state_id]
        name = state_names.get(state_id, state["path"].stem)
        lon, lat = state_centroid_latlon(state, centroids, rbf_lon, rbf_lat)
        culture_id, rule = assign_culture(state_id, name, lon, lat, state["path"].name)
        assignments[state_id] = (culture_id, rule)
        assignment_rows.append(
            {
                "lotd_state_id": state_id,
                "name": name,
                "lat": f"{lat:.4f}" if lat is not None else "",
                "lon": f"{lon:.4f}" if lon is not None else "",
                "nationality_id": culture_id,
                "culture_name": CULTURE_LABELS.get(culture_id, "No Culture" if culture_id == 0 else str(culture_id)),
                "rule_used": rule,
            }
        )

    write_assignments_csv(assignment_rows)
    write_culture_effect(assignments)

    unassigned = [row for row in assignment_rows if int(row["nationality_id"]) == 0]
    print(f"Wrote {len(assignment_rows)} culture assignments to {OUTPUT_CSV}")
    print(f"Wrote scripted effect to {EFFECT_FILE}")
    print(f"Wrote TNO reference ({len(tno_rows)} rows) to {TNO_REFERENCE_CSV}")
    print(f"Unassigned states: {len(unassigned)}")
    if unassigned:
        for row in unassigned[:15]:
            print(f"  - {row['lotd_state_id']}: {row['name']} ({row['rule_used']})")


if __name__ == "__main__":
    main()
