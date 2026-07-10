#!/usr/bin/env python3
"""Build history/units OOB files from PYTHON_OOB_* lines in game.log."""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "history" / "states"
STATE_NAMES_LOC = ROOT / "localisation" / "english" / "state_names_l_english.yml"
DEFAULT_LOG = Path.home() / "Documents/Paradox Interactive/Hearts of Iron IV/logs/game.log"
OUT_DIR = ROOT / "history" / "units"

SCRIPT_NAMES = {
    "1": "d_create_army",
    "2": "d_create_army2",
    "3": "d_create_army3",
}

TEMPLATE_BLOCKS = {
    "Infantry Unit": '''division_template = {
\tname = "Infantry Unit"
\tregiments = {
\t\tinfantry = { x = 0 y = 0 }
\t\tinfantry = { x = 0 y = 1 }
\t\tinfantry = { x = 0 y = 2 }
\t\tinfantry = { x = 1 y = 0 }
\t\tinfantry = { x = 1 y = 1 }
\t\tinfantry = { x = 1 y = 2 }
\t}
\tsupport = {
\t\tartillery = { x = 0 y = 0 }
\t\tengineer = { x = 0 y = 1 }
\t}
}''',
    "Infantry Division": '''division_template = {
\tname = "Infantry Division"
\tdivision_names_group = USA_INF_01
\tregiments = {
\t\tinfantry = { x = 0 y = 0 }
\t\tinfantry = { x = 0 y = 1 }
\t\tinfantry = { x = 0 y = 2 }
\t\tinfantry = { x = 1 y = 0 }
\t\tinfantry = { x = 1 y = 1 }
\t\tinfantry = { x = 1 y = 2 }
\t\tinfantry = { x = 2 y = 0 }
\t\tinfantry = { x = 2 y = 1 }
\t\tinfantry = { x = 2 y = 2 }
\t}
\tsupport = {
\t\tengineer = { x = 0 y = 0 }
\t\tartillery = { x = 0 y = 1 }
\t\tanti_tank = { x = 0 y = 2 }
\t}
}''',
    "Motorized Division": '''division_template = {
\tname = "Motorized Division"
\tdivision_names_group = USA_MOT_01
\tregiments = {
\t\tmotorized = { x = 0 y = 0 }
\t\tmotorized = { x = 0 y = 1 }
\t\tmotorized = { x = 0 y = 2 }
\t\tmotorized = { x = 1 y = 0 }
\t\tmotorized = { x = 1 y = 1 }
\t\tmotorized = { x = 1 y = 2 }
\t\tMBT = { x = 2 y = 0 }
\t\tMBT = { x = 2 y = 1 }
\t\tMBT = { x = 2 y = 2 }
\t}
\tsupport = {
\t\tartillery = { x = 0 y = 0 }
\t\tengineer = { x = 0 y = 1 }
\t\tsignal_company = { x = 0 y = 2 }
\t}
}''',
}

TEMPLATE_ID_MAP = {
    "1": "Infantry Unit",
    "2": "Infantry Division",
    "3": "Motorized Division",
    "Infantry Unit": "Infantry Unit",
    "Infantry Division": "Infantry Division",
    "Motorized Division": "Motorized Division",
}


@dataclass
class StateSpawn:
    state_name: str
    state_id: int | None = None
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


@dataclass
class CountryRun:
    tag: str
    name: str
    script_id: str
    planned: int = 0
    spawned: int = 0
    states: dict[int, StateSpawn] = field(default_factory=dict)


def extract_payload(line: str, marker: str) -> str | None:
    """HOI4 sometimes nests log lines; take the last PYTHON_OOB_* payload."""
    idx = line.rfind(marker)
    if idx < 0:
        return None
    return line[idx:].strip()


def load_state_name_to_id() -> dict[str, int]:
    if not STATE_NAMES_LOC.exists():
        return {}
    mapping: dict[str, int] = {}
    for match in re.finditer(r'STATE_(\d+):\d*\s+"([^"]+)"', STATE_NAMES_LOC.read_text(encoding="utf-8", errors="replace")):
        mapping[match.group(2)] = int(match.group(1))
    return mapping


def load_state_locations() -> dict[int, int]:
    """Map state id -> best province id for OOB location (highest VP, else first province)."""
    locations: dict[int, int] = {}
    vp_re = re.compile(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)\s*\}")
    province_re = re.compile(r"provinces\s*=\s*\{([^}]+)\}")
    id_re = re.compile(r"^\s*id\s*=\s*(\d+)\s*$", re.M)

    for path in sorted(STATE_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        state_id_match = id_re.search(text)
        if not state_id_match:
            continue
        state_id = int(state_id_match.group(1))
        best_province = None
        best_vp = -1
        for prov, vp in vp_re.findall(text):
            vp_val = int(vp)
            if vp_val > best_vp:
                best_vp = vp_val
                best_province = int(prov)
        if best_province is None:
            prov_match = province_re.search(text)
            if prov_match:
                provinces = [int(x) for x in prov_match.group(1).split()]
                if provinces:
                    best_province = provinces[0]
        if best_province is not None:
            locations[state_id] = best_province
    return locations


def parse_log(path: Path) -> dict[tuple[str, str], CountryRun]:
    runs: dict[tuple[str, str], CountryRun] = {}
    current: CountryRun | None = None

    begin_re = re.compile(
        r"PYTHON_OOB_BEGIN;([^;]*);([^;]*);script=([^;]*);manpower_k=([^;]*);planned_divs=([^;]*)"
    )
    state_re = re.compile(
        r"PYTHON_OOB_STATE;([^;]*);script=([^;]*);state=([^;]*);total=([^;]*);infantry_unit=([^;]*);infantry_division=([^;]*);motorized_division=([^;]*)"
    )
    state_re_legacy = re.compile(
        r"PYTHON_OOB_STATE;([^;]*);script=([^;]*);state_id=([^;]*);state=([^;]*);total=([^;]*);infantry_unit=([^;]*);infantry_division=([^;]*);motorized_division=([^;]*)"
    )
    end_re = re.compile(
        r"PYTHON_OOB_END;([^;]*);([^;]*);script=([^;]*);spawned=([^;]*);planned=([^;]*);country_divisions=([^;]*)"
    )
    end_re_legacy = re.compile(
        r"PYTHON_OOB_END;([^;]*);script=([^;]*);spawned=([^;]*);planned=([^;]*);country_divisions=([^;]*)"
    )

    def finalize_current() -> None:
        nonlocal current
        if current is None or not current.states:
            current = None
            return
        if not current.tag:
            current = None
            return
        runs[(current.tag, current.script_id)] = current
        current = None

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        payload = (
            extract_payload(raw_line, "PYTHON_OOB_BEGIN;")
            or extract_payload(raw_line, "PYTHON_OOB_STATE;")
            or extract_payload(raw_line, "PYTHON_OOB_END;")
        )
        if not payload:
            continue

        begin_match = begin_re.search(payload)
        if begin_match:
            finalize_current()
            tag, name, script_id, _manpower_k, planned = begin_match.groups()
            current = CountryRun(
                tag=tag.strip(),
                name=name.strip(),
                script_id=script_id.strip(),
                planned=int(float(planned or 0)),
            )
            continue

        state_match = state_re.search(payload) or state_re_legacy.search(payload)
        if state_match:
            if state_re.search(payload):
                tag, script_id, state_name, _total, iu, idiv, mot = state_match.groups()
            else:
                tag, script_id, _state_id, state_name, _total, iu, idiv, mot = state_match.groups()
            tag = tag.strip()
            script_id = script_id.strip()
            state_name = state_name.strip()
            if current is None:
                current = CountryRun(tag=tag, name="", script_id=script_id)
            if not current.tag and tag:
                current.tag = tag
            if current.script_id != script_id and not current.states:
                current.script_id = script_id
            entry = current.states.setdefault(state_name, StateSpawn(state_name=state_name))
            for count, template in (
                (iu, "Infantry Unit"),
                (idiv, "Infantry Division"),
                (mot, "Motorized Division"),
            ):
                value = int(float(count or 0))
                if value:
                    entry.counts[template] += value
            continue

        end_match = end_re.search(payload) or end_re_legacy.search(payload)
        if end_match:
            if end_re.search(payload):
                tag, name, script_id, spawned, _planned, _country_divs = end_match.groups()
            else:
                tag, script_id, spawned, _planned, _country_divs = end_match.groups()
                name = ""
            if current is not None:
                if not current.tag and tag.strip():
                    current.tag = tag.strip()
                if not current.name and name.strip():
                    current.name = name.strip()
                current.spawned = int(float(spawned or 0))
            finalize_current()

    finalize_current()
    return runs


def render_oob(
    run: CountryRun,
    state_locations: dict[int, int],
    state_name_to_id: dict[str, int],
) -> str:
    templates_used: set[str] = set()
    unit_lines: list[str] = []

    for state_name in sorted(run.states):
        state = run.states[state_name]
        state_id = state.state_id or state_name_to_id.get(state_name)
        location = state_locations.get(state_id, 1) if state_id else 1
        comment = state_name
        for template, count in sorted(state.counts.items()):
            templates_used.add(template)
            for _ in range(count):
                unit_lines.append(
                    f"\tdivision = {{\n"
                    f"\t\tlocation = {location} #{comment}\n"
                    f"\t\tdivision_template = \"{template}\"\n"
                    f"\t\tstart_experience_factor = 0.5\n"
                    f"\t\tstart_equipment_factor = 0.75\n"
                    f"\t}}"
                )

    template_blocks = "\n".join(TEMPLATE_BLOCKS[t] for t in ("Infantry Unit", "Infantry Division", "Motorized Division") if t in templates_used)
    script_name = SCRIPT_NAMES.get(run.script_id, f"script_{run.script_id}")
    header = (
        f"# Generated from game.log by parse_oob_gen_log.py\n"
        f"# Country: {run.tag} ({run.name})\n"
        f"# Source: {script_name}\n"
        f"# Planned divisions: {run.planned}, logged spawns: {run.spawned}\n"
    )
    body = "units = {\n" + "\n".join(unit_lines) + "\n}"
    return header + "\n" + template_blocks + "\n\n" + body + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse PYTHON_OOB_* game.log lines into history/units files.")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="Path to game.log")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="Output directory for generated OOB files")
    parser.add_argument("--tag", help="Only emit OOB for this country tag")
    parser.add_argument("--dry-run", action="store_true", help="Print summary only, do not write files")
    args = parser.parse_args()

    if not args.log.exists():
        raise SystemExit(f"Log not found: {args.log}")

    state_locations = load_state_locations()
    state_name_to_id = load_state_name_to_id()
    runs = parse_log(args.log)
    if not runs:
        raise SystemExit("No PYTHON_OOB_* lines found in log. Run the game once after army generation.")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for (tag, script_id), run in sorted(runs.items()):
        if args.tag and tag != args.tag:
            continue
        if not run.states:
            continue
        out_path = args.out_dir / f"{tag}_2005.txt"
        content = render_oob(run, state_locations, state_name_to_id)
        if args.dry_run:
            print(f"{tag} script={script_id} states={len(run.states)} spawned={run.spawned} -> {out_path.name}")
        else:
            out_path.write_text(content, encoding="utf-8")
            print(f"Wrote {out_path} ({run.spawned} divisions across {len(run.states)} states)")
            written += 1

    if not args.dry_run:
        print(f"Done. {written} OOB file(s) in {args.out_dir}")


if __name__ == "__main__":
    main()
