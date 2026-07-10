#!/usr/bin/env python3
"""Assign AssocRegion tokens to LOTD states by US state / CA province / MX estado."""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

from assign_infrastructure import (
    POPULATION_CSV,
    ROOT,
    STATE_DIR,
    load_state_names,
    parse_definition,
    parse_states,
    rasterize_provinces,
)
from assign_state_cultures import (
    CARIBBEAN_PATTERNS,
    MEXICO_ABBR_DEFAULT,
    MEXICO_NAME_DEFAULT,
    NYC_BOROUGH_STATE_IDS,
    US_REGION_BOXES,
    US_STATE_DEFAULT,
    US_STATE_FULL_NAME_TO_ABBR,
    build_inverse_rbf,
    disambiguate_abbr,
    match_canada_province,
    match_name_patterns,
    normalize_name,
    parse_trailing_abbr,
    state_centroid_latlon,
)
from assign_victory_points import (
    compute_province_centroids,
    load_or_create_anchor_snapshot,
    parse_known_vp_localisations,
)

OUTPUT_CSV = ROOT / "assoc_region_assignments.csv"
IDEAS_FILE = ROOT / "common/ideas/admin_title_ideas.txt"
LOC_FILE = ROOT / "localisation/english/admin_title_l_english.yml"
INIT_FILE = ROOT / "common/scripted_effects/admin_title_scripted_effects.txt"
EFFECT_FILE = ROOT / "common/scripted_effects/admin_title_LOTD_effects.txt"
VP_LOCALISATION = ROOT / "localisation/english/TNO_victory_points_l_english.yml"
PROVINCES_BMP = ROOT / "map/provinces.bmp"
DEFINITION_CSV = ROOT / "map/definition.csv"

WORLD_BORDER_STATE = 26

CUBA_STATE_IDS = frozenset({
    698, 731, 693, 694, 691, 685, 699, 686, 696, 700, 702, 708, 736, 711, 96, 722, 734,
})
HAITI_STATE_IDS = frozenset({
    729, 732, 726, 736, 735, 744, 760, 765, 769, 773,
})
SKIP_STATE_IDS = CUBA_STATE_IDS | HAITI_STATE_IDS | {WORLD_BORDER_STATE}

EXISTING_REGION_TOKENS = frozenset({
    "Assoc_Region_British_West_Indies",
    "Assoc_Region_Cibao",
    "Assoc_Region_East",
    "Assoc_Region_South",
    "Assoc_Region_Lesser_Antilles",
    "Assoc_Region_Greater_Antilles",
    "Assoc_Region_Michigan",
})

US_ABBR_TO_SUFFIX: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New_Hampshire", "NJ": "New_Jersey", "NM": "New_Mexico",
    "NY": "New_York", "NC": "North_Carolina", "ND": "North_Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode_Island", "SC": "South_Carolina",
    "SD": "South_Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West_Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District_of_Columbia", "PR": "Puerto_Rico",
}

US_SUFFIX_TO_DISPLAY: dict[str, str] = {
    "Alabama": "Alabama", "Alaska": "Alaska", "Arizona": "Arizona", "Arkansas": "Arkansas",
    "California": "California", "Colorado": "Colorado", "Connecticut": "Connecticut",
    "Delaware": "Delaware", "Florida": "Florida", "Georgia": "Georgia", "Hawaii": "Hawaii",
    "Idaho": "Idaho", "Illinois": "Illinois", "Indiana": "Indiana", "Iowa": "Iowa",
    "Kansas": "Kansas", "Kentucky": "Kentucky", "Louisiana": "Louisiana", "Maine": "Maine",
    "Maryland": "Maryland", "Massachusetts": "Massachusetts", "Michigan": "Michigan",
    "Minnesota": "Minnesota", "Mississippi": "Mississippi", "Missouri": "Missouri",
    "Montana": "Montana", "Nebraska": "Nebraska", "Nevada": "Nevada",
    "New_Hampshire": "New Hampshire", "New_Jersey": "New Jersey", "New_Mexico": "New Mexico",
    "New_York": "New York", "North_Carolina": "North Carolina", "North_Dakota": "North Dakota",
    "Ohio": "Ohio", "Oklahoma": "Oklahoma", "Oregon": "Oregon", "Pennsylvania": "Pennsylvania",
    "Rhode_Island": "Rhode Island", "South_Carolina": "South Carolina",
    "South_Dakota": "South Dakota", "Tennessee": "Tennessee", "Texas": "Texas", "Utah": "Utah",
    "Vermont": "Vermont", "Virginia": "Virginia", "Washington": "Washington",
    "West_Virginia": "West Virginia", "Wisconsin": "Wisconsin", "Wyoming": "Wyoming",
    "District_of_Columbia": "District of Columbia", "Puerto_Rico": "Puerto Rico",
}

MEXICO_ABBR_TO_SUFFIX: dict[str, str] = {
    "AG": "Aguascalientes", "BC": "Baja_California", "BS": "Baja_California_Sur", "CM": "Campeche",
    "CS": "Chiapas", "CH": "Chihuahua", "CO": "Coahuila", "CL": "Colima", "DF": "Mexico_City",
    "DG": "Durango", "GT": "Guanajuato", "GR": "Guerrero", "HG": "Hidalgo", "JA": "Jalisco",
    "EM": "Mexico", "MI": "Michoacan", "MO": "Morelos", "NA": "Nayarit", "NL": "Nuevo_Leon",
    "OA": "Oaxaca", "PU": "Puebla", "QT": "Queretaro", "QR": "Quintana_Roo", "SL": "San_Luis_Potosi",
    "SI": "Sinaloa", "SO": "Sonora", "TB": "Tabasco", "TL": "Tlaxcala", "TM": "Tamaulipas",
    "VE": "Veracruz", "YU": "Yucatan", "ZA": "Zacatecas", "MX": "Mexico", "CP": "Chiapas",
    "VC": "Veracruz", "TA": "Tamaulipas",
}

MEXICO_SUFFIX_TO_DISPLAY: dict[str, str] = {
    "Aguascalientes": "Aguascalientes",
    "Baja_California": "Baja California",
    "Baja_California_Sur": "Baja California Sur",
    "Campeche": "Campeche",
    "Chiapas": "Chiapas",
    "Chihuahua": "Chihuahua",
    "Coahuila": "Coahuila",
    "Colima": "Colima",
    "Mexico_City": "Mexico City",
    "Durango": "Durango",
    "Guanajuato": "Guanajuato",
    "Guerrero": "Guerrero",
    "Hidalgo": "Hidalgo",
    "Tlaxcala": "Tlaxcala",
    "Jalisco": "Jalisco",
    "Mexico": "Mexico",
    "Michoacan": "Michoacan",
    "Morelos": "Morelos",
    "Nayarit": "Nayarit",
    "Nuevo_Leon": "Nuevo Leon",
    "Oaxaca": "Oaxaca",
    "Puebla": "Puebla",
    "Queretaro": "Queretaro",
    "Quintana_Roo": "Quintana Roo",
    "San_Luis_Potosi": "San Luis Potosi",
    "Sinaloa": "Sinaloa",
    "Sonora": "Sonora",
    "Tabasco": "Tabasco",
    "Tlaxcala": "Tlaxcala",
    "Tamaulipas": "Tamaulipas",
    "Veracruz": "Veracruz",
    "Yucatan": "Yucatan",
    "Zacatecas": "Zacatecas",
}

CANADA_SUFFIX_TO_DISPLAY: dict[str, str] = {
    "Quebec": "Quebec",
    "Ontario": "Ontario",
    "British_Columbia": "British Columbia",
    "Alberta": "Alberta",
    "Saskatchewan": "Saskatchewan",
    "Manitoba": "Manitoba",
    "Nova_Scotia": "Nova Scotia",
    "New_Brunswick": "New Brunswick",
    "Prince_Edward_Island": "Prince Edward Island",
    "Newfoundland_and_Labrador": "Newfoundland and Labrador",
    "Yukon": "Yukon",
    "Northwest_Territories": "Northwest Territories",
    "Nunavut": "Nunavut",
    "Saint_Pierre_et_Miquelon": "Saint-Pierre-et-Miquelon",
    "Guantanamo_Bay": "Guantanamo Bay",
}

DR_CIBAO_NAMES = (
    "santiago, do", "puerto plata", "monte cristi", "la vega", "dajabon", "duarte",
    "maria trinidad", "valverde", "santiago rodriguez", "monte plata", "espaillat",
)
DR_EAST_NAMES = (
    "samana", "la altagracia", "el seybo", "hato mayor", "maria trinidad sanchez",
)
DR_SOUTH_NAMES = (
    "santo domingo", "san pedro", "la romana", "azua", "la estrelleta", "peravia",
    "barahona", "san cristobal", "san juan, do", "distrito nacional",
)


def suffix_to_token(suffix: str) -> str:
    return f"Assoc_Region_{suffix}"


def match_canada_region(name: str) -> str | None:
    normalized = normalize_name(name)
    if "saint-pierre" in normalized or "saint pierre" in normalized or "miquelon" in normalized:
        return suffix_to_token("Saint_Pierre_et_Miquelon")
    if "guantanamo" in normalized:
        return suffix_to_token("Guantanamo_Bay")
    if ", qc" in normalized or normalized.endswith(" qc") or normalized == "quebec":
        return suffix_to_token("Quebec")
    if ", on" in normalized or normalized.endswith(" on") or normalized == "ontario":
        return suffix_to_token("Ontario")
    if ", bc" in normalized or "british columbia" in normalized:
        return suffix_to_token("British_Columbia")
    if ", ab" in normalized or "alberta" in normalized:
        return suffix_to_token("Alberta")
    if ", sk" in normalized or "saskatchewan" in normalized or "saskatchwan" in normalized:
        return suffix_to_token("Saskatchewan")
    if ", mb" in normalized or "manitoba" in normalized:
        return suffix_to_token("Manitoba")
    if ", ns" in normalized or "nova scotia" in normalized:
        return suffix_to_token("Nova_Scotia")
    if ", nb" in normalized or "new brunswick" in normalized:
        return suffix_to_token("New_Brunswick")
    if "prince edward" in normalized or ", pe" in normalized:
        return suffix_to_token("Prince_Edward_Island")
    if "newfoundland" in normalized or "labrador" in normalized:
        return suffix_to_token("Newfoundland_and_Labrador")
    if "nunavut" in normalized or ", nu" in normalized:
        return suffix_to_token("Nunavut")
    if "northwest territories" in normalized or ", nt" in normalized:
        return suffix_to_token("Northwest_Territories")
    if "yukon" in normalized or ", yt" in normalized:
        return suffix_to_token("Yukon")
    return None


def match_mexico_region(name: str, abbr: str | None) -> str | None:
    normalized = normalize_name(name)
    if abbr and abbr.startswith("MX_"):
        mx_abbr = abbr[3:]
        suffix = MEXICO_ABBR_TO_SUFFIX.get(mx_abbr)
        if suffix:
            return suffix_to_token(suffix)
    if abbr and abbr in MEXICO_ABBR_DEFAULT:
        suffix = MEXICO_ABBR_TO_SUFFIX.get(abbr)
        if suffix:
            return suffix_to_token(suffix)
    for needles, _ in MEXICO_NAME_DEFAULT:
        if any(needle in normalized for needle in needles):
            first = needles[0]
            mapping = {
                "baja california sur": "Baja_California_Sur",
                "baja california": "Baja_California",
                "sonora": "Sonora",
                "chihuahua": "Chihuahua",
                "coahuila": "Coahuila",
                "nuevo leon": "Nuevo_Leon",
                "tamaulipas": "Tamaulipas",
                "monterrey": "Nuevo_Leon",
                "matamoros": "Tamaulipas",
                "laredos": "Tamaulipas",
                "sinaloa": "Sinaloa",
                "los mochis": "Sinaloa",
                "mazatlan": "Sinaloa",
                "durango": "Durango",
                "zacatecas": "Zacatecas",
                "aguascalientes": "Aguascalientes",
                "fresnillo": "Zacatecas",
                "gomez palacio": "Durango",
                "san luis potosi": "San_Luis_Potosi",
                "veracruz": "Veracruz",
                "jalisco": "Jalisco",
                "nayarit": "Nayarit",
                "colima": "Colima",
                "puerto vallarta": "Jalisco",
                "michoacan": "Michoacan",
                "guanajuato": "Guanajuato",
                "queretaro": "Queretaro",
                "morelia": "Michoacan",
                "morelos": "Morelos",
                "mexico city": "Mexico_City",
                "tenochtitlan": "Mexico_City",
                "toluca": "Mexico",
                "puebla": "Puebla",
                "guerrero": "Guerrero",
                "oaxaca": "Oaxaca",
                "chiapas": "Chiapas",
                "tabasco": "Tabasco",
                "yucatan": "Yucatan",
                "quintana roo": "Quintana_Roo",
                "campeche": "Campeche",
                "merida": "Yucatan",
                "cancun": "Quintana_Roo",
                "hidalgo, mx": "Hidalgo",
            }
            for key, suffix in mapping.items():
                if key in first or first in key:
                    return suffix_to_token(suffix)
    return None


def match_dr_region(name: str, lat: float | None) -> str:
    normalized = normalize_name(name)
    for needle in DR_SOUTH_NAMES:
        if needle in normalized:
            return "Assoc_Region_South"
    for needle in DR_EAST_NAMES:
        if needle in normalized:
            return "Assoc_Region_East"
    for needle in DR_CIBAO_NAMES:
        if needle in normalized:
            return "Assoc_Region_Cibao"
    if lat is not None:
        if lat < 18.85:
            return "Assoc_Region_South"
        if lat >= 19.35:
            return "Assoc_Region_Cibao"
        return "Assoc_Region_East"
    return "Assoc_Region_East"


def us_suffix_from_abbr(abbr: str) -> str | None:
    return US_ABBR_TO_SUFFIX.get(abbr)


def assign_assoc_region(
    state_id: int,
    name: str,
    lon: float | None,
    lat: float | None,
    filename: str,
) -> tuple[str | None, str, str | None]:
    """Return (region_token, rule, assoc_region_2_token)."""
    if state_id in SKIP_STATE_IDS:
        return None, "skipped_existing", None

    normalized = normalize_name(name)

    if state_id in NYC_BOROUGH_STATE_IDS or any(
        token in filename.lower() for token in ("bronx", "brooklyn", "queens", "staten island")
    ):
        return suffix_to_token("New_York"), "nyc_borough", None

    if ", cu" in normalized or normalized.endswith(" cu"):
        return "Assoc_Region_Greater_Antilles", "cuba_name", None

    if ", ht" in normalized or normalized.endswith(" ht") or "haiti" in normalized:
        return "Assoc_Region_Greater_Antilles", "haiti_name", None

    if normalized == "hidalgo" and lat is not None and lat < 30:
        return suffix_to_token("Hidalgo"), "mexico_hidalgo_state", None

    if normalized == "da":
        return suffix_to_token("Florida"), "florida_da_name", None

    if normalized == "grand island":
        return suffix_to_token("New_York"), "grand_island_ny", None

    if "virgin islands" in normalized:
        return "Assoc_Region_Lesser_Antilles", "virgin_islands", None

    if normalized == "district of columbia":
        return suffix_to_token("District_of_Columbia"), "dc_name", None

    if ", nyc" in normalized or normalized.startswith("manhattan"):
        return suffix_to_token("New_York"), "nyc_name", None

    if normalized in US_STATE_FULL_NAME_TO_ABBR:
        abbr = US_STATE_FULL_NAME_TO_ABBR[normalized]
        suffix = us_suffix_from_abbr(abbr)
        if suffix:
            return suffix_to_token(suffix), "us_full_state_name", None

    if "guantanamo" in normalized:
        return suffix_to_token("Guantanamo_Bay"), "guantanamo_name", None

    if any(needle in normalized for needle in (", bs", "bahamas", "nassau", "freeport", "eleuthera", "andros")):
        return "Assoc_Region_British_West_Indies", "bahamas_name", None

    if ", do" in normalized or normalized.endswith(" do"):
        token = match_dr_region(name, lat)
        return token, "dr_region", "Assoc_Region_Greater_Antilles"

    canada = match_canada_region(name)
    if canada:
        return canada, "canada_name", None

    mexico = match_mexico_region(name, None)
    if mexico:
        return mexico, "mexico_name", None

    abbr = parse_trailing_abbr(name)
    if abbr:
        if abbr == "NYC":
            return suffix_to_token("New_York"), "nyc_abbr", None
        resolved = disambiguate_abbr(abbr, name, lat)
        if resolved.startswith("MX_"):
            token = match_mexico_region(name, resolved)
            if token:
                return token, "mexico_abbr", None
        if resolved.startswith("CARIB_"):
            if ", pr" in normalized or "puerto rico" in normalized:
                return suffix_to_token("Puerto_Rico"), "puerto_rico_abbr", None
            return "Assoc_Region_British_West_Indies", "caribbean_abbr", None
        if resolved == "PR":
            return suffix_to_token("Puerto_Rico"), "puerto_rico", None
        if resolved in US_STATE_DEFAULT:
            suffix = us_suffix_from_abbr(resolved)
            if suffix:
                return suffix_to_token(suffix), "us_state_abbr", None
        if resolved in MEXICO_ABBR_DEFAULT:
            token = match_mexico_region(name, f"MX_{resolved}")
            if token:
                return token, "mexico_abbr_direct", None

    if lon is not None and lat is not None:
        effective_abbr = abbr if abbr and not str(abbr).startswith(("MX_", "CARIB_")) else None
        for box in US_REGION_BOXES:
            if box.matches(lon, lat, effective_abbr):
                for us_abbr, culture_id in US_STATE_DEFAULT.items():
                    if culture_id == box.culture_id and (box.states is None or us_abbr in box.states):
                        suffix = us_suffix_from_abbr(us_abbr)
                        if suffix:
                            return suffix_to_token(suffix), f"geo_box_{box.rule}", None
                break

    if abbr and abbr in US_STATE_DEFAULT:
        suffix = us_suffix_from_abbr(abbr)
        if suffix:
            return suffix_to_token(suffix), "us_state_default", None

    if lon is not None and lat is not None:
        if lat >= 49 and lon <= -95:
            region = match_canada_region(name)
            return region or suffix_to_token("Nunavut"), "northern_canada_lat", None
        if 14 <= lat <= 33 and lon <= -105:
            token = match_mexico_region(name, None)
            if token:
                return token, "mexico_lat_fallback", None

    caribbean = match_name_patterns(name, CARIBBEAN_PATTERNS)
    if caribbean is not None:
        if caribbean == 90:
            return suffix_to_token("Puerto_Rico"), "caribbean_pr", None
        if caribbean == 96:
            return "Assoc_Region_British_West_Indies", "caribbean_bs", None
        if caribbean == 102:
            token = match_dr_region(name, lat)
            return token, "caribbean_do", "Assoc_Region_Greater_Antilles"

    canada_fallback = match_canada_province(name)
    if canada_fallback is not None:
        region = match_canada_region(name)
        if region:
            return region, "canada_culture_fallback", None

    return None, "unassigned", None


def region_display_name(token: str) -> str:
    if token in {
        "Assoc_Region_British_West_Indies",
        "Assoc_Region_Cibao",
        "Assoc_Region_East",
        "Assoc_Region_South",
        "Assoc_Region_Lesser_Antilles",
        "Assoc_Region_Greater_Antilles",
    }:
        return token.removeprefix("Assoc_Region_").replace("_", " ")
    suffix = token.removeprefix("Assoc_Region_")
    if suffix in US_SUFFIX_TO_DISPLAY:
        return US_SUFFIX_TO_DISPLAY[suffix]
    if suffix in MEXICO_SUFFIX_TO_DISPLAY:
        return MEXICO_SUFFIX_TO_DISPLAY[suffix]
    if suffix in CANADA_SUFFIX_TO_DISPLAY:
        return CANADA_SUFFIX_TO_DISPLAY[suffix]
    return suffix.replace("_", " ")


def format_state_or_block(state_ids: list[int], indent: str = "\t\t\t") -> list[str]:
    lines = [f"{indent}OR = {{"]
    for state_id in sorted(state_ids):
        lines.append(f"{indent}\tstate = {state_id}")
    lines.append(f"{indent}}}")
    return lines


def generate_effects_block(token: str, state_ids: list[int]) -> list[str]:
    label = region_display_name(token)
    lines = [f"\t# {label}", "\tevery_state = {", "\t\tlimit = {"]
    lines.extend(format_state_or_block(state_ids))
    lines.append("\t\t}")
    lines.append(f"\t\tset_variable = {{ AssocRegion = token:{token} }}")
    lines.append("\t}")
    lines.append("")
    return lines


def patch_ideas_file(new_tokens: list[str]) -> None:
    text = IDEAS_FILE.read_text(encoding="utf-8")
    missing = [token for token in sorted(new_tokens) if f"{token} =" not in text]
    if not missing:
        return
    additions = "\n".join(generate_idea_entry(token) for token in missing)
    marker_end = (
        "\t\tAssoc_Region_Michigan = {\n"
        "\t\t\ton_add = {\n"
        "\t\t\t\tremove_ideas = Assoc_Region_Michigan\n"
        "\t\t\t\tset_temp_variable = { i = token:Assoc_Region_Michigan }\n"
        "\t\t\t}\n"
        "\t\t}"
    )
    if marker_end not in text:
        raise SystemExit("Could not find Assoc_Region_Michigan block in ideas file")
    text = text.replace(marker_end, marker_end + "\n" + additions, 1)
    IDEAS_FILE.write_text(text, encoding="utf-8")


def patch_loc_file(new_tokens: list[str]) -> None:
    text = LOC_FILE.read_text(encoding="utf-8-sig")
    lines = []
    for token in sorted(new_tokens):
        if f"{token}:" in text:
            continue
        display = region_display_name(token)
        lines.append(f'{token}: "{display}"')
    if not lines:
        return
    marker = "Assoc_Region_Michigan: \"Michigan\""
    if marker not in text:
        raise SystemExit("Could not find Assoc_Region_Michigan loc marker")
    insert_at = text.index(marker) + len(marker)
    block = "\n" + "\n".join(lines)
    text = text[:insert_at] + block + text[insert_at:]
    LOC_FILE.write_text(text, encoding="utf-8-sig")


def patch_init_file(new_tokens: list[str]) -> None:
    text = INIT_FILE.read_text(encoding="utf-8")
    additions = "\n".join(
        f"\tadd_ideas = {token}" for token in sorted(new_tokens) if f"add_ideas = {token}" not in text
    )
    if not additions:
        return
    marker = "\tadd_ideas = Assoc_Region_Michigan"
    if marker not in text:
        raise SystemExit("Could not find Assoc_Region_Michigan init marker")
    text = text.replace(marker, marker + additions, 1)
    INIT_FILE.write_text(text, encoding="utf-8")


def generate_idea_entry(token: str) -> str:
    return f"""\t\t{token} = {{
\t\t\ton_add = {{
\t\t\t\tremove_ideas = {token}
\t\t\t\tset_temp_variable = {{ i = token:{token} }}
\t\t\t}}
\t\t}}"""


def patch_lotd_effects(primary_blocks: list[str], dr_state_ids: list[int]) -> None:
    text = EFFECT_FILE.read_text(encoding="utf-8")
    cuba_marker = "\t#Cuba"
    if cuba_marker not in text:
        raise SystemExit("Could not find #Cuba marker in LOTD effects file")

    header = """TNO_admin_title_LOTD = { 
\t#Sorted by country alphabetically and Admin Titles are set in this order:
\t##Admin Titles
\t##Assoc Regions
\t##Sub-Admin Titles
\t##Subdivisions

"""
    body = "\n".join(primary_blocks)
    if dr_state_ids:
        body += "\t# Dominican Republic — secondary Greater Antilles association\n"
        body += "\tevery_state = {\n"
        body += "\t\tlimit = {\n"
        body += "\n".join(format_state_or_block(dr_state_ids))
        body += "\n\t\t}\n"
        body += "\t\tset_variable = { AssocRegion2 = token:Assoc_Region_Greater_Antilles }\n"
        body += "\t}\n\n"

    tail = text[text.index(cuba_marker):]
    EFFECT_FILE.write_text(header + body + tail, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign AssocRegion tokens to LOTD states.")
    parser.add_argument("--apply", action="store_true", help="Write generated content into mod files")
    args = parser.parse_args()

    color_to_province, _ = parse_definition(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    centroids = compute_province_centroids(province_raster)
    known_vps = parse_known_vp_localisations(VP_LOCALISATION)
    pairs = load_or_create_anchor_snapshot(known_vps, centroids)
    rbf_lon, rbf_lat = build_inverse_rbf(pairs)

    states = parse_states(STATE_DIR)
    state_names = load_state_names(POPULATION_CSV)

    rows: list[dict] = []
    grouped: dict[str, list[int]] = defaultdict(list)
    grouped2: dict[str, list[int]] = defaultdict(list)

    for state_id in sorted(states):
        state = states[state_id]
        name = state_names.get(state_id, state["path"].stem)
        lon, lat = state_centroid_latlon(state, centroids, rbf_lon, rbf_lat)
        token, rule, token2 = assign_assoc_region(state_id, name, lon, lat, state["path"].name)
        rows.append(
            {
                "state_id": state_id,
                "name": name,
                "lat": f"{lat:.4f}" if lat is not None else "",
                "lon": f"{lon:.4f}" if lon is not None else "",
                "region_token": token or "",
                "assoc_region_2": token2 or "",
                "rule_used": rule,
            }
        )
        if token:
            grouped[token].append(state_id)
        if token2:
            grouped2[token2].append(state_id)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["state_id", "name", "lat", "lon", "region_token", "assoc_region_2", "rule_used"],
        )
        writer.writeheader()
        writer.writerows(rows)

    new_tokens = sorted(token for token in grouped if token not in EXISTING_REGION_TOKENS)
    effect_blocks: list[str] = []
    for token in sorted(grouped, key=lambda item: region_display_name(item).lower()):
        effect_blocks.extend(generate_effects_block(token, grouped[token]))

    dr_assoc2_ids = sorted(grouped2.get("Assoc_Region_Greater_Antilles", []))

    print(f"Wrote {len(rows)} assignments to {OUTPUT_CSV}")
    print(f"Regions assigned: {len(grouped)} (new ideas needed: {len(new_tokens)})")
    print(f"Skipped / unassigned: {sum(1 for r in rows if not r['region_token'])}")
    for sample in ("Assoc_Region_Michigan", "Assoc_Region_Texas", "Assoc_Region_Quebec", "Assoc_Region_Sonora"):
        if sample in grouped:
            print(f"  {sample}: {len(grouped[sample])} states")

    if args.apply:
        if new_tokens:
            patch_ideas_file(new_tokens)
            patch_loc_file(new_tokens)
            patch_init_file(new_tokens)
        patch_lotd_effects(effect_blocks, dr_assoc2_ids)
        print(f"Patched ideas ({len(new_tokens)} new), localization, init, and LOTD effects.")
    else:
        print("Dry run only. Re-run with --apply to patch mod files.")


if __name__ == "__main__":
    main()
