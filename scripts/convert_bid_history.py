"""
One-time converter: extracts the "PS1 - Bid History" sheet from the hackathon
sample dataset xlsx into app/data/bid_history.csv (the file the scoring engine
loads at startup).

Usage:  python scripts/convert_bid_history.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
XLSX = ROOT / "Problem#1_Sample_Datasets (TEKROWE).xlsx"
OUT = ROOT / "app" / "data" / "bid_history.csv"


def main() -> None:
    # Sheet 0 = "PS1 - Bid History"; rows 1-2 are a title banner, headers on row 3
    df = pd.read_excel(XLSX, sheet_name=0, header=2)
    df = df.dropna(how="all")

    print(f"Shape: {df.shape}")
    print(df.head(3).to_string())
    print("\nOutcome counts:")
    print(df["Outcome"].value_counts())
    print("\nSector win rates:")
    print(df.groupby("Sector")["Outcome"].apply(lambda s: (s == "Win").mean()).round(3))

    df.to_csv(OUT, index=False, encoding="utf-8")
    print(f"\nWrote {len(df)} rows -> {OUT}")


if __name__ == "__main__":
    main()
