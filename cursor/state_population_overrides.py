"""Authoritative population overrides for states the grid pipeline mis-assigns."""
from __future__ import annotations

# state_id -> population (2000-era canon)
MANUAL_STATE_POPULATIONS: dict[int, int] = {
    210: 4,  # Isle Royale, MI — uninhabited park
    737: 130_000,  # La Romana, DO
    738: 195_000,  # San Pedro de Macoris, DO
    757: 874_963,  # Quintana Roo, MX
}

# state_id -> forced category (optional; applied by assign_state_categories)
MANUAL_STATE_CATEGORIES: dict[int, str] = {
    210: "wasteland",
}
