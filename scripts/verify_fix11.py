"""
Fix 11 verification — fastembed swap (dim, normalisation, rankings, RSS).
Run:  venv\\Scripts\\python.exe scripts\\verify_fix11.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)

import numpy as np  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── 1. Embedding interface ───────────────────────────────────────────────────
from app.services.capability_store import embed_texts, search_capabilities_hybrid  # noqa: E402

v = embed_texts(["ISO 27001 cloud infrastructure"])
check("shape (1, 384)", v.shape == (1, 384), f"(got {v.shape})")
check("dtype float32", v.dtype == np.float32)
check("L2-normalised", abs(float(np.linalg.norm(v[0])) - 1.0) < 1e-5,
      f"(norm={np.linalg.norm(v[0]):.6f})")

batch = embed_texts(["alpha", "beta", "gamma"])
check("batch shape (3, 384)", batch.shape == (3, 384))

# ── 2. Retrieval rankings preserved ──────────────────────────────────────────
iso = search_capabilities_hybrid(
    "Bidder must hold ISO 27001 certification for cybersecurity operations", top_k=5)
print("\n  ISO query top-5:")
for r in iso:
    print(f"    {r['_rank']}. {r['id']} {r['domain']:<20} hybrid={r['_hybrid']:.3f} cert={r.get('certification')}")
top4 = iso[:4]
check("top-4 are ISO 27001 Cybersecurity caps",
      all(r["domain"] == "Cybersecurity" and r.get("certification") == "ISO 27001" for r in top4))

road = search_capabilities_hybrid("Construction of 25km dual carriageway road", top_k=5)
print("\n  Road query top-5:")
for r in road:
    print(f"    {r['_rank']}. {r['id']} {r['domain']:<20} hybrid={r['_hybrid']:.3f}")
check("road query rank-1 is Road Construction", road[0]["domain"] == "Road Construction")
check("no Bridge Engineering above Road Construction",
      all(r["domain"] == "Road Construction" for r in road[:3]))

spread = iso[0]["_hybrid"] - iso[2]["_hybrid"]
check("score spread retained", iso[0]["_hybrid"] != iso[4]["_hybrid"])

# ── 3. Memory footprint ──────────────────────────────────────────────────────
try:
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    rss_mb = pmc.WorkingSetSize / (1024 * 1024)
    print(f"\n  Process RSS with model + index loaded: {rss_mb:.0f} MB")
    check("RSS under 500 MB", rss_mb < 500, f"({rss_mb:.0f} MB)")
except Exception as exc:
    print(f"  (RSS check skipped: {exc})")

# ── 4. torch must not be loaded ──────────────────────────────────────────────
check("torch not imported anywhere in the embedding path", "torch" not in sys.modules)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
