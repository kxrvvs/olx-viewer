from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from .export import to_csv, to_json
from .scraper import SORT_OPTIONS, search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape OLX.pt search results to CSV or JSON.")
    parser.add_argument("query", help="Search term, e.g. 'iphone 13'")
    parser.add_argument("-p", "--pages", type=int, default=1, help="Number of result pages to scrape (default: 1)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output file path (.csv or .json)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between page requests (default: 1.0)")

    parser.add_argument(
        "--category",
        help="Restrict to an OLX category — partial name or path (e.g. 'telemoveis'). "
             "If the term matches more than one category, the error message lists the exact paths to choose from.",
    )
    parser.add_argument("--min-price", type=float, help="Minimum price filter")
    parser.add_argument("--max-price", type=float, help="Maximum price filter")
    parser.add_argument("--condition", choices=["new", "used"], help="Filter by item condition")
    parser.add_argument("--sort", choices=sorted(SORT_OPTIONS), help="Sort order for results")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    suffix = args.output.suffix.lower()
    if suffix not in (".csv", ".json"):
        parser.error("--output must end in .csv or .json")

    try:
        listings = search(
            args.query,
            max_pages=args.pages,
            delay=args.delay,
            category=args.category,
            min_price=args.min_price,
            max_price=args.max_price,
            condition=args.condition,
            sort=args.sort,
        )
    except ValueError as exc:
        parser.error(str(exc))
    except requests.RequestException as exc:
        parser.error(f"Network error: {exc}")

    if suffix == ".csv":
        to_csv(listings, args.output)
    else:
        to_json(listings, args.output)

    print(f"Saved {len(listings)} listings to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
