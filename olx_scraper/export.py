from __future__ import annotations

import csv
import json
from pathlib import Path

from .scraper import Listing

FIELDNAMES = ["id", "title", "price", "city", "district", "date", "url", "image_url"]


def to_csv(listings: list[Listing], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for listing in listings:
            writer.writerow(listing.as_dict())


def to_json(listings: list[Listing], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([listing.as_dict() for listing in listings], fh, ensure_ascii=False, indent=2)
