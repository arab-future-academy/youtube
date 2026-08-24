#!/usr/bin/env python3
"""Regenerate GitHub Pages files from the local SQLite master database."""

from argparse import ArgumentParser
from pathlib import Path

from catalog_db import export_static_catalog


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = ArgumentParser()
    parser.add_argument("--database", type=Path, default=root / "data" / "youtube.sqlite3")
    parser.add_argument("--output", type=Path, default=root / "data" / "youtube.json")
    parser.add_argument("--thumbnails", type=Path, default=root / "assets" / "thumbnails")
    args = parser.parse_args()

    export_static_catalog(args.database, args.output, args.thumbnails)
    print(f"Exported {args.output}")
    print(f"Exported thumbnail images to {args.thumbnails}")


if __name__ == "__main__":
    main()
