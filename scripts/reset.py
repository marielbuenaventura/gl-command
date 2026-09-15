#!/usr/bin/env python3
"""Drop the database and the document vault, then optionally reseed.

    python scripts/reset.py            # empty database, no demo data
    python scripts/reset.py --seed     # empty database, reseeded with demo data
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glcmd import config, db, seed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the GL-Command database.")
    parser.add_argument("--seed", action="store_true", help="reload the demo dataset")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if not args.yes:
        answer = input(f"This deletes {config.DB_PATH} and every stored document. "
                       "Type 'reset' to continue: ")
        if answer.strip().lower() != "reset":
            print("Aborted.")
            return 1

    db.reset()
    if config.VAULT_DIR.exists():
        shutil.rmtree(config.VAULT_DIR)
    config.VAULT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Removed {config.DB_PATH} and the document vault.")

    conn = db.connect()
    db.init_db(conn)
    print("Schema recreated.")

    if args.seed:
        result = seed.seed_all(conn)
        print(f"Seeded periods {', '.join(result['periods'])}:")
        for k, v in result["counts"].items():
            print(f"  {k:<18} {v:>6,}")
    else:
        print("Database is empty. Load your own trial balance from the Flux Analysis page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
