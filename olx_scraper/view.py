from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from html import escape
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests

from .export import to_json
from .scraper import SORT_OPTIONS, numeric_price, search

IMAGE_TEMPLATE = '<img src="{image_url}" alt="{title}" loading="lazy">'
NO_IMAGE_TEMPLATE = '<div class="no-image">No pic</div>'

CARD_TEMPLATE = """\
    <a class="card" href="{url}" target="_blank" rel="noopener noreferrer" data-price="{price_value}" data-date="{date}">
      {media}
      <p class="price">{price}</p>
      <p class="district">{district}</p>
      <p class="date">{date}</p>
    </a>
"""

PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <title>OLX listings</title>
  <style>
    body {{ font-family: sans-serif; background: #f4f4f4; margin: 0; padding: 1.5rem; }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 1rem;
      margin-bottom: 1rem;
    }}
    h1 {{ margin: 0; }}
    .toolbar select {{ font-size: 1rem; padding: 0.3rem 0.5rem; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 1rem;
    }}
    .card {{
      display: block;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
      color: inherit;
      text-decoration: none;
    }}
    .card img, .card .no-image {{
      display: block;
      width: 100%;
      height: 160px;
      background: #ddd;
    }}
    .card img {{
      object-fit: cover;
    }}
    .card .no-image {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: #888;
      font-size: 0.9rem;
    }}
    .card p {{ margin: 0.4rem 0.6rem; }}
    .price {{ font-weight: bold; }}
    .district, .date {{ color: #666; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>{count} listings</h1>
    <div class="toolbar">
      <label for="sort">Sort by:</label>
      <select id="sort">
        <option value="">Default order</option>
        <option value="price-asc">Price: Low to High</option>
        <option value="price-desc">Price: High to Low</option>
        <option value="date-asc">Date: Oldest First</option>
        <option value="date-desc">Date: Newest First</option>
      </select>
    </div>
  </div>
  <div class="grid">
{cards}
  </div>
  <script>
    document.getElementById("sort").addEventListener("change", function () {{
      var grid = document.querySelector(".grid");
      var cards = Array.from(grid.children);
      var parts = this.value.split("-");
      var key = parts[0];
      var direction = parts[1];
      if (!key) {{
        return;
      }}

      cards.sort(function (a, b) {{
        var valueA, valueB;
        if (key === "price") {{
          valueA = parseFloat(a.dataset.price);
          valueB = parseFloat(b.dataset.price);
          if (isNaN(valueA)) valueA = direction === "asc" ? Infinity : -Infinity;
          if (isNaN(valueB)) valueB = direction === "asc" ? Infinity : -Infinity;
        }} else {{
          valueA = a.dataset.date;
          valueB = b.dataset.date;
        }}
        if (valueA < valueB) return direction === "asc" ? -1 : 1;
        if (valueA > valueB) return direction === "asc" ? 1 : -1;
        return 0;
      }});

      cards.forEach(function (card) {{
        grid.appendChild(card);
      }});
    }});
  </script>
</body>
</html>
"""


def load_listings(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def price_attr(price: str) -> str:
    """Render `numeric_price` as a `data-price` attribute value.

    Unparseable prices (e.g. "Sob consulta") become "" — `parseFloat("")` is
    `NaN`, and the page's sort script already pushes `NaN` prices to the end.
    """
    value = numeric_price(price)
    return "" if value is None else str(value)


def render_media(listing: dict) -> str:
    image_url = listing.get("image_url")
    if not image_url:
        return NO_IMAGE_TEMPLATE
    return IMAGE_TEMPLATE.format(image_url=escape(image_url), title=escape(listing.get("title", "")))


def render_html(listings: list[dict]) -> str:
    cards = "".join(
        CARD_TEMPLATE.format(
            url=escape(listing.get("url", "")),
            media=render_media(listing),
            price=escape(listing.get("price", "")),
            price_value=price_attr(listing.get("price", "")),
            district=escape(listing.get("district", "")),
            date=escape(listing.get("date", "")),
        )
        for listing in listings
    )
    return PAGE_TEMPLATE.format(count=len(listings), cards=cards)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open scraped OLX listings as a browsable HTML page.")
    parser.add_argument("input", type=Path, help="Path to a JSON file produced by the scraper")
    parser.add_argument(
        "-q", "--query",
        help="If given, scrape this search term first and overwrite `input` with the fresh results "
             "before opening the page (e.g. -q \"iphone 13\")",
    )
    parser.add_argument(
        "-p", "--pages", type=int, default=1,
        help="Number of result pages to scrape (default: 1, only used with --query)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Seconds to wait between page requests (default: 1.0, only used with --query)",
    )

    parser.add_argument(
        "--category",
        help="Restrict to an OLX category — partial name or path (only used with --query). "
             "If the term matches more than one category, the error message lists the exact paths to choose from.",
    )
    parser.add_argument("--min-price", type=float, help="Minimum price filter (only used with --query)")
    parser.add_argument("--max-price", type=float, help="Maximum price filter (only used with --query)")
    parser.add_argument("--condition", choices=["new", "used"], help="Filter by item condition (only used with --query)")
    parser.add_argument("--sort", choices=sorted(SORT_OPTIONS), help="Sort order for results (only used with --query)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.query:
        try:
            scraped = search(
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
        to_json(scraped, args.input)
        print(f"Scraped {len(scraped)} listings into {args.input}")

    listings = load_listings(args.input)
    html = render_html(listings)

    with NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as fh:
        fh.write(html)
        output_path = Path(fh.name)

    webbrowser.open(output_path.resolve().as_uri())
    print(f"Opened {len(listings)} listings from {args.input}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
