"""Consolidated portfolio builder script for Portfolio-Dashboard.

Synchronizes data/holdings.csv with tags, writes the Excel master
Portfolio_Holdings.xlsx, and updates offline fallback files (initial_holdings.json & seed_data.js).
"""
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CANONICAL_CSV = DATA_DIR / "holdings.csv"
TAGS_FILE = DATA_DIR / "tags.csv"
EXCEL_FILE = ROOT / "Portfolio_Holdings.xlsx"


def main():
    if not CANONICAL_CSV.exists():
        print(f"[ERROR] {CANONICAL_CSV} not found!")
        return 1

    df = pd.read_csv(CANONICAL_CSV, dtype=str).fillna("")
    df = df.where(pd.notnull(df), "")

    if TAGS_FILE.exists():
        try:
            tags_df = pd.read_csv(TAGS_FILE, dtype=str).fillna("")
            if "Lot ID" in tags_df.columns:
                tag_map = tags_df.set_index("Lot ID").to_dict(orient="index")
                for idx, row in df.iterrows():
                    lot_id = str(row.get("Lot ID", "")).strip()
                    if lot_id in tag_map:
                        t = tag_map[lot_id]
                        for field in ["Owner", "Broker", "Tag1"]:
                            if field in t and t[field]:
                                df.at[idx, field] = t[field]
        except Exception as e:
            print(f"[WARN] Error applying tags: {e}")

    # Write updated CSV
    df.to_csv(CANONICAL_CSV, index=False)

    # Export to Excel
    try:
        df.to_excel(EXCEL_FILE, index=False, sheet_name="Holdings")
        print(f"Exported {len(df)} rows to {EXCEL_FILE.name}")
    except Exception as e:
        print(f"[WARN] Could not write Excel file: {e}")

    # Update initial_holdings.json & seed_data.js
    records = df.to_dict(orient="records")
    clean_records = []
    for r in records:
        clean_r = {k: ("" if pd.isna(v) else str(v)) for k, v in r.items()}
        clean_records.append(clean_r)

    try:
        with open(ROOT / "initial_holdings.json", "w", encoding="utf-8") as f:
            json.dump(clean_records, f, indent=2)
    except Exception as e:
        print(f"[WARN] Error writing initial_holdings.json: {e}")

    for seed_path in [ROOT / "seed_data.js", ROOT / "static" / "js" / "seed_data.js"]:
        if seed_path.parent.exists():
            try:
                seed_obj = {"holdings": clean_records}
                seed_path.write_text("window.MERIDIAN_SEED_DATA = " + json.dumps(seed_obj, indent=2) + ";\n", encoding="utf-8")
            except Exception as e:
                print(f"[WARN] Error writing {seed_path.name}: {e}")

    print(f"Successfully processed {len(df)} portfolio holdings.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
