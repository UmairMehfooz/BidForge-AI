"""
BidForge AI — Bid History Service
==================================
Loads the historical bid outcomes dataset (120 rows from the hackathon
"PS1 — Bid History" sheet, converted to app/data/bid_history.csv) and exposes
sector-level win-rate statistics used by the scoring engine and the
win-probability model.

Public API
----------
    df = load_bid_history()                 # cached pandas DataFrame (or None)
    rate = get_sector_win_rate("Energy")    # → 0.65 (falls back to overall)
    stats = get_overall_stats()             # → overall + per-sector win rates
    sector = infer_sector_from_text(text)   # keyword fallback for sector

Design decisions
----------------
- Module-level singleton, loaded once at startup (same pattern as the FAISS
  capability store). 120 rows → negligible memory and load time.
- Budget strings ("PKR 422M") are parsed into numeric millions at load time
  so the win model can use them directly as a feature.
- If the CSV is missing, every accessor degrades gracefully (overall fallback
  win rate, empty stats) and a warning is logged — startup never crashes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
_DATA_DIR = Path(__file__).parent.parent / "data"
_CSV_PATH = _DATA_DIR / "bid_history.csv"

# Overall win rate of the 120-row dataset (68 wins / 120 bids) — used as the
# fallback when the CSV is missing or a sector has never been seen.
FALLBACK_WIN_RATE = 0.567

# Canonical sector names present in the dataset (used for fuzzy lookups and
# keyword inference).
KNOWN_SECTORS = [
    "Construction",
    "Education",
    "Energy",
    "Finance",
    "Healthcare",
    "IT Services",
    "Logistics",
    "Telecom",
]

# Keyword map for inferring an RFP's sector from raw text when the LLM did
# not provide one. Checked in order; first sector with the most hits wins.
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    "Construction": ["construction", "road", "carriageway", "bridge", "civil works", "building", "infrastructure works", "highway"],
    "Education":    ["education", "school", "university", "campus", "learning management", "student", "curriculum"],
    "Energy":       ["energy", "power plant", "solar", "grid", "electricity", "transmission line", "megawatt", "renewable"],
    "Finance":      ["bank", "finance", "financial services", "payment", "fintech", "insurance", "treasury"],
    "Healthcare":   ["hospital", "health", "medical", "clinic", "patient", "pharma", "telemedicine"],
    "IT Services":  ["software", "cloud", "data center", "datacenter", "erp", "it services", "network infrastructure", "cybersecurity", "application development", "system integration"],
    "Logistics":    ["logistics", "warehouse", "fleet", "transport", "supply chain", "freight", "cargo"],
    "Telecom":      ["telecom", "fiber", "fibre", "5g", "lte", "base station", "broadband", "telecommunication"],
}


# ---------------------------------------------------------------------------
# Budget parsing — "PKR 422M" → 422.0 (numeric millions)
# ---------------------------------------------------------------------------

def parse_budget_millions(budget_str: Any) -> float | None:
    """
    Parse a budget string like "PKR 422M", "Rs. 1.2B", "PKR 50 million"
    into numeric MILLIONS. Returns None if unparseable.
    """
    if budget_str is None:
        return None

    text = str(budget_str).strip().upper()
    text = re.sub(r"(PKR|RS\.?|RUPEES?|USD)\s*", "", text).strip()

    match = re.search(r"([\d,.]+)\s*(B|BILLION|M|MILLION|K|THOUSAND|CR|CRORE|L|LAKH)?", text)
    if not match:
        return None

    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None

    multiplier_to_millions = {
        "B": 1_000, "BILLION": 1_000,
        "M": 1, "MILLION": 1,
        "K": 0.001, "THOUSAND": 0.001,
        "CR": 10, "CRORE": 10,
        "L": 0.1, "LAKH": 0.1,
        "": 0.000001,   # plain number assumed to be raw PKR
    }
    mult = multiplier_to_millions.get((match.group(2) or "").upper(), 1)
    return round(number * mult, 4)


# ---------------------------------------------------------------------------
# Loader (module-level singleton)
# ---------------------------------------------------------------------------
_df: pd.DataFrame | None = None
_load_attempted = False


def load_bid_history() -> pd.DataFrame | None:
    """
    Load app/data/bid_history.csv into a cached DataFrame.

    Adds a derived column:
        budget_m  — Budget parsed into numeric millions
        won       — 1 if Outcome == "Win" else 0

    Returns None (with a logged warning) if the CSV is missing or unreadable —
    callers must handle the None case.
    """
    global _df, _load_attempted
    if _df is not None or _load_attempted:
        return _df
    _load_attempted = True

    if not _CSV_PATH.exists():
        logger.warning(
            "bid_history.csv not found at %s — scoring falls back to pure heuristics.",
            _CSV_PATH,
        )
        return None

    try:
        df = pd.read_csv(_CSV_PATH)
        df["budget_m"] = df["Budget"].apply(parse_budget_millions)
        df["won"] = (df["Outcome"].str.strip().str.lower() == "win").astype(int)
        _df = df
        logger.info(
            "Bid history loaded: %d rows, %d sectors, overall win rate %.3f",
            len(df), df["Sector"].nunique(), df["won"].mean(),
        )
    except Exception as exc:
        logger.warning("Failed to load bid_history.csv (%s) — falling back to heuristics.", exc)
        _df = None

    return _df


def reload_bid_history() -> pd.DataFrame | None:
    """
    Drop the cached DataFrame and reload from disk — used after a new
    bid_history.csv has been uploaded so stats and the win model refresh
    without a server restart.
    """
    global _df, _load_attempted
    _df, _load_attempted = None, False
    return load_bid_history()


# ---------------------------------------------------------------------------
# Sector win rates
# ---------------------------------------------------------------------------

def get_sector_win_rate(sector: str | None) -> float:
    """
    Historical win rate for `sector` (case-insensitive). Falls back to the
    overall dataset win rate when the sector is unseen or history is missing.
    """
    df = load_bid_history()
    if df is None or not sector:
        return FALLBACK_WIN_RATE

    mask = df["Sector"].str.strip().str.lower() == str(sector).strip().lower()
    subset = df[mask]
    if subset.empty:
        logger.info("Sector '%s' not in bid history — using overall win rate.", sector)
        return round(float(df["won"].mean()), 4)

    return round(float(subset["won"].mean()), 4)


def get_overall_stats() -> dict[str, Any]:
    """
    Aggregate statistics over the full bid history:
        overall_win_rate, total_bids, win_rate_by_sector,
        avg_score_wins, avg_score_losses
    """
    df = load_bid_history()
    if df is None:
        return {
            "overall_win_rate": FALLBACK_WIN_RATE,
            "total_bids": 0,
            "win_rate_by_sector": {},
            "avg_score_wins": None,
            "avg_score_losses": None,
        }

    by_sector = (
        df.groupby("Sector")["won"].mean().round(4).to_dict()
    )
    wins = df[df["won"] == 1]
    losses = df[df["won"] == 0]

    return {
        "overall_win_rate": round(float(df["won"].mean()), 4),
        "total_bids": int(len(df)),
        "win_rate_by_sector": by_sector,
        "avg_score_wins": round(float(wins["Score (%)"].mean()), 2) if not wins.empty else None,
        "avg_score_losses": round(float(losses["Score (%)"].mean()), 2) if not losses.empty else None,
    }


# ---------------------------------------------------------------------------
# Sector inference fallback (keyword-based, no LLM)
# ---------------------------------------------------------------------------

def infer_sector_from_text(text: str) -> str | None:
    """
    Infer the most likely sector from raw RFP / requirement text by counting
    keyword hits. Returns None if nothing matches — callers fall back to the
    overall win rate in that case.
    """
    if not text:
        return None

    lowered = text.lower()
    best_sector, best_hits = None, 0
    for sector, keywords in _SECTOR_KEYWORDS.items():
        hits = sum(lowered.count(kw) for kw in keywords)
        if hits > best_hits:
            best_sector, best_hits = sector, hits

    return best_sector
