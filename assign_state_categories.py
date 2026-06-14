#!/usr/bin/env python3
"""Assign state_category in history/states/*.txt based on population and name rules."""
import argparse
import csv
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT_CSV = ROOT / "state_population_estimates.csv"
STATE_CATEGORY_DIR = ROOT / "common" / "state_category"

VALID_CATEGORIES = {
    "wasteland",
    "enclave",
    "tiny_island",
    "small_island",
    "pastoral",
    "rural",
    "town",
    "large_town",
    "city",
    "large_city",
    "metropolis",
    "megalopolis",
}

ISLAND_PATTERN = re.compile(
    r"\b(island|islands|isla|islas|ile|iles)\b",
    re.IGNORECASE,
)

NATIONAL_PARK_PATTERN = re.compile(
    r"\b(national park|yellowstone|yosemite|sequoia|capitol reef)\b",
    re.IGNORECASE,
)

POPULATION_THRESHOLDS = [
    (1, 999, "enclave"),
    (1_000, 4_999, "pastoral"),
    (5_000, 19_999, "rural"),
    (20_000, 59_999, "town"),
    (60_000, 124_999, "large_town"),
    (125_000, 299_999, "city"),
    (300_000, 599_999, "large_city"),
    (600_000, 1_499_999, "metropolis"),
    (1_500_000, 10**12, "megalopolis"),
]


def parse_population(value):
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_island_name(name):
    return bool(ISLAND_PATTERN.search(name or ""))


def is_wasteland_name(name):
    key = (name or "").strip().lower()
    if key == "world border":
        return True
    return bool(NATIONAL_PARK_PATTERN.search(name or ""))


def category_from_population(population):
    for low, high, category in POPULATION_THRESHOLDS:
        if low <= population <= high:
            return category
    return "wasteland"


def assign_category(name, population):
    if population == 0 or is_wasteland_name(name):
        return "wasteland"

    if is_island_name(name):
        if population < 1_000:
            return "tiny_island"
        if population <= 24_999:
            return "small_island"

    return category_from_population(population)


def read_csv_rows(csv_path):
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def update_state_category(file_path, category):
    with open(file_path, encoding="utf-8") as f:
        text = f.read()

    if not re.search(r"^\s*state_category\s*=\s*\w+\s*$", text, flags=re.MULTILINE):
        raise ValueError(f"Missing state_category line in {file_path}")

    new_text = re.sub(
        r"^(\s*state_category\s*=\s*)\w+\s*$",
        rf"\1{category}",
        text,
        count=1,
        flags=re.MULTILINE,
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_text)


def load_valid_categories_from_files():
    categories = set()
    for path in STATE_CATEGORY_DIR.glob("*.txt"):
        categories.add(path.stem)
    return categories


def parse_args():
    parser = argparse.ArgumentParser(
        description="Assign HOI4 state categories based on population estimates."
    )
    parser.add_argument(
        "--input",
        default=str(INPUT_CSV),
        help="CSV file with state population estimates",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply category changes back into state files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed summary output",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    rows = read_csv_rows(Path(args.input))
    file_categories = load_valid_categories_from_files()

    assignments = []
    category_counts = Counter()
    island_samples = []
    wasteland_samples = []

    for row in rows:
        name = row.get("name", "")
        population = parse_population(row.get("estimated_population"))
        category = assign_category(name, population)

        if category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{category}' for state {row.get('id')}: {name}")
        if category not in file_categories:
            raise ValueError(
                f"Category '{category}' has no file in common/state_category/ "
                f"(state {row.get('id')}: {name})"
            )

        assignments.append(
            {
                "id": row.get("id"),
                "file": row.get("file"),
                "name": name,
                "population": population,
                "category": category,
            }
        )
        category_counts[category] += 1

        if category in {"tiny_island", "small_island"} and len(island_samples) < 15:
            island_samples.append(assignments[-1])
        if category == "wasteland" and len(wasteland_samples) < 15:
            wasteland_samples.append(assignments[-1])

    print(f"Processed {len(assignments)} states from {args.input}")
    print("\nCategory distribution:")
    for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {category:>14}: {count}")

    print("\nIsland category samples:")
    for row in island_samples:
        print(
            f"  {row['id']:>4} {row['name']:<40} pop={row['population']:<10} -> {row['category']}"
        )

    print("\nWasteland samples:")
    for row in wasteland_samples:
        print(
            f"  {row['id']:>4} {row['name']:<40} pop={row['population']:<10} -> {row['category']}"
        )

    if args.verbose:
        print("\nSpot checks:")
        spot_ids = {"4", "775", "17", "26"}
        for row in assignments:
            if row["id"] in spot_ids:
                print(
                    f"  {row['id']:>4} {row['name']:<40} pop={row['population']:<10} -> {row['category']}"
                )

    if args.apply:
        changed = 0
        for row in assignments:
            path = ROOT / row["file"]
            with open(path, encoding="utf-8") as f:
                current = f.read()
            match = re.search(
                r"^\s*state_category\s*=\s*(\w+)\s*$",
                current,
                flags=re.MULTILINE,
            )
            current_category = match.group(1) if match else None
            if current_category == row["category"]:
                continue
            update_state_category(path, row["category"])
            changed += 1
        print(f"\nApplied category updates to {changed} state files.")
    else:
        print("\nDry run only. Re-run with --apply to write changes.")


if __name__ == "__main__":
    main()
