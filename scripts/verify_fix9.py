"""
Fix 9 verification — effort metrics math (unit) .
Run:  venv\\Scripts\\python.exe scripts\\verify_fix9.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.routers.workspaces import _build_effort_metrics  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


# 95.3s total vs 6h baseline → 1 - 95.3/21600 = 99.6% reduction
m = _build_effort_metrics({"pipeline_timings": {"parse": 41.2, "match": 18.7, "score": 0.4, "total": 95.3}})
check("automated_minutes 1.6", m["automated_minutes"] == 1.6, f"(got {m['automated_minutes']})")
check("baseline 6.0h", m["manual_baseline_hours"] == 6.0)
check("reduction 99.6%", m["reduction_percent"] == 99.6, f"(got {m['reduction_percent']})")

# internal consistency: reduction == 1 - automated/(baseline)
expected = round((1 - 95.3 / (6.0 * 3600)) * 100, 1)
check("reduction formula consistent", m["reduction_percent"] == expected)

# no timings → nulls, never crashes
empty = _build_effort_metrics({"pipeline_timings": None})
check("no timings -> nulls", empty["automated_minutes"] is None and empty["reduction_percent"] is None)
check("baseline still reported", empty["manual_baseline_hours"] == 6.0)

# pathological: automated longer than baseline clamps to 0, not negative
slow = _build_effort_metrics({"pipeline_timings": {"total": 7 * 3600.0}})
check("slower-than-baseline clamps to 0", slow["reduction_percent"] == 0.0, f"(got {slow['reduction_percent']})")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
