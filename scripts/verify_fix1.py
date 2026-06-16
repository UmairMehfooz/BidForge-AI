"""
Fix 1 verification — bid history loading + win-probability model.
Run:  venv\\Scripts\\python.exe scripts\\verify_fix1.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.services.bid_history import (  # noqa: E402
    get_overall_stats,
    get_sector_win_rate,
    infer_sector_from_text,
    load_bid_history,
    parse_budget_millions,
)
from app.services.win_model import (  # noqa: E402
    get_model_insights,
    predict_win_probability,
    train_win_model,
)

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── 1. Bid history loads ────────────────────────────────────────────────────
df = load_bid_history()
check("CSV loads with 120 rows", df is not None and len(df) == 120,
      f"(rows={0 if df is None else len(df)})")

# ── 2. Budget parsing ───────────────────────────────────────────────────────
check("parse 'PKR 422M' -> 422", parse_budget_millions("PKR 422M") == 422)
check("parse 'Rs. 1.5B' -> 1500", parse_budget_millions("Rs. 1.5B") == 1500)
check("parse garbage -> None", parse_budget_millions("N/A") is None)

# ── 3. Sector win rates ─────────────────────────────────────────────────────
energy = get_sector_win_rate("Energy")
education = get_sector_win_rate("Education")
unseen = get_sector_win_rate("Space Tourism")
check("Energy win rate ~0.65", abs(energy - 0.65) < 0.01, f"(got {energy})")
check("Education win rate ~0.43", abs(education - 0.43) < 0.01, f"(got {education})")
check("Unseen sector -> overall ~0.567", abs(unseen - 0.567) < 0.01, f"(got {unseen})")

stats = get_overall_stats()
check("Overall stats present", stats["total_bids"] == 120 and len(stats["win_rate_by_sector"]) == 8,
      f"(overall={stats['overall_win_rate']}, wins avg score={stats['avg_score_wins']}, "
      f"losses avg score={stats['avg_score_losses']})")

# ── 4. Sector keyword inference ─────────────────────────────────────────────
sector = infer_sector_from_text("Construction of 25km dual carriageway road including bridges")
check("Keyword inference -> Construction", sector == "Construction", f"(got {sector})")

# ── 5. Model trains fast and is accurate ────────────────────────────────────
t0 = time.perf_counter()
trained = train_win_model()
elapsed = time.perf_counter() - t0
check("Model trains", trained)
check("Training < 2s", elapsed < 2.0, f"({elapsed:.3f}s)")

insights = get_model_insights()
check("Train accuracy ~1.0 (separable data — expected)",
      insights.get("train_accuracy", 0) >= 0.99, f"(got {insights.get('train_accuracy')})")
check("score_pct is dominant coefficient",
      insights.get("dominant_feature") == "score_pct",
      f"(coefficients={insights.get('coefficients')})")
check("Decision boundary surfaced",
      insights.get("decision_boundary", {}).get("perfectly_separable") is True,
      f"(boundary={insights.get('decision_boundary', {}).get('min_winning_score')})")

# ── 6. Predictions behave sensibly ──────────────────────────────────────────
strong = predict_win_probability({"score_pct": 90, "compliance_pct": 85, "gaps_found": 1,
                                  "budget_m": 200, "sector_win_rate": 0.65})
weak = predict_win_probability({"score_pct": 40, "compliance_pct": 50, "gaps_found": 8,
                                "budget_m": 200, "sector_win_rate": 0.43})
check("Strong bid -> high probability", strong is not None and strong > 0.8, f"(got {strong})")
check("Weak bid -> low probability", weak is not None and weak < 0.2, f"(got {weak})")
check("Missing features imputed (no crash)",
      predict_win_probability({"score_pct": 75}) is not None)

# ── 7. Graceful degradation when the CSV is missing ─────────────────────────
import app.services.bid_history as bh  # noqa: E402
import app.services.win_model as wm  # noqa: E402

bh._df, bh._load_attempted = None, False
original = bh._CSV_PATH
bh._CSV_PATH = Path("does/not/exist.csv")
check("Missing CSV -> fallback win rate", get_sector_win_rate("Energy") == bh.FALLBACK_WIN_RATE)
wm._pipeline = None
check("Missing CSV -> train returns False (no crash)", train_win_model() is False)
check("Untrained model -> predict returns None", predict_win_probability({"score_pct": 75}) is None)
bh._CSV_PATH = original
bh._df, bh._load_attempted = None, False

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
