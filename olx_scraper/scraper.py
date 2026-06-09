from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass

import requests

PRICE_NUMBER_RE = re.compile(r"[\d.,]+")

BASE_URL = "https://www.olx.pt/ads/"
CATEGORY_URL = "https://www.olx.pt/{path}/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PRERENDERED_STATE_MARKER = 'window.__PRERENDERED_STATE__= "'

# Friendly --sort names mapped to OLX's own `search[order]` values. Price sorting
# and date sorting are the two axes OLX exposes; "asc"/"desc" read naturally for
# price (cheapest/priciest first) but for dates we use "oldest"/"newest" instead,
# since "ascending date" is a less intuitive way to ask for "oldest first".
SORT_OPTIONS = {
    "price-asc": "filter_float_price:asc",
    "price-desc": "filter_float_price:desc",
    "newest": "created_at:desc",
    "oldest": "created_at:asc",
}


@dataclass
class Listing:
    id: str
    title: str
    price: str
    city: str
    district: str
    date: str
    url: str
    image_url: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def numeric_price(price: str) -> float | None:
    """Pull a sortable number out of a display price like "1.199 €".

    OLX formats prices the pt-PT way: "." groups thousands, "," marks decimals.
    Strip everything else and swap those separators so callers (the GUI's and
    HTML viewer's sort features) can compare prices as plain floats. Non-numeric
    prices (e.g. "Sob consulta") yield `None`, letting callers push those listings
    to the end of a sort instead of guessing at a value for them.
    """
    match = PRICE_NUMBER_RE.search(price)
    if not match:
        return None
    return float(match.group().replace(".", "").replace(",", "."))


def fetch_page(
    query: str,
    page: int,
    session: requests.Session,
    *,
    category_path: str | None = None,
    extra_params: dict[str, object] | None = None,
) -> str:
    url = CATEGORY_URL.format(path=category_path) if category_path else BASE_URL
    params = {"q": query, "page": page}
    if extra_params:
        params.update(extra_params)

    response = session.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    return response.text


def fetch_categories(session: requests.Session) -> dict[str, dict]:
    """Fetch OLX's full category tree.

    Every search-results page embeds the *entire* category tree (id -> category
    dict, with each entry's URL `path` slug) in its prerendered state — regardless
    of which category or query the page itself is for. So any lightweight fetch
    doubles as a way to obtain it.
    """
    html = fetch_page("", 1, session)
    state = extract_prerendered_state(html)
    return state["categories"]["list"]


def find_category(categories: dict[str, dict], name: str) -> dict:
    """Resolve a (partial, case-insensitive) category name to its OLX entry.

    OLX addresses categories by URL path slug (e.g. "telemoveis-e-tablets/telemoveis"),
    not by an id you can pass as a query parameter — so the only way to target one is
    to know its path. We match the user's text against every category's display name
    and path, and ask them to pick a specific path when more than one fits.
    """
    # OLX's own category URLs look like "https://www.olx.pt/{path}/" — copying a
    # path from one (or typing it the same way) leaves a trailing slash that
    # wouldn't match the slash-less paths stored in `categories`.
    needle = name.strip().strip("/").casefold()

    # An exact path match (e.g. one copy-pasted from a previous "ambiguous" error)
    # always wins outright — otherwise it would itself look ambiguous, since it's
    # also a *substring* of each of its own subcategories' paths.
    for category in categories.values():
        if category["path"].casefold() == needle:
            return category

    matches = [
        category for category in categories.values()
        if needle in category["name"].casefold() or needle in category["path"].casefold()
    ]

    if not matches:
        raise ValueError(f"No OLX category matches {name!r} — try a different or shorter search term")

    if len(matches) > 1:
        matches.sort(key=lambda category: category["path"])
        shown = matches[:20]
        lines = [f"  {category['path']}  ({category['name']})" for category in shown]
        if len(matches) > len(shown):
            lines.append(f"  ... and {len(matches) - len(shown)} more — try a more specific term")
        raise ValueError(
            f"{name!r} matches multiple OLX categories — pass one of these paths instead:\n" + "\n".join(lines)
        )

    return matches[0]


def fetch_regions(session: requests.Session) -> list[dict]:
    """Fetch OLX's list of districts (what OLX's own data calls "regions").

    Unlike categories, OLX doesn't embed a full location taxonomy on every
    page — districts only show up as a "facet" (id, label, result count) on a
    generic search-results page, so a lightweight empty-query fetch doubles as
    a way to read them off `metaData.facets.region`, mirroring `fetch_categories`.
    """
    html = fetch_page("", 1, session)
    state = extract_prerendered_state(html)
    facets = state["listing"]["listing"]["metaData"].get("facets") or {}
    return [{"id": region["id"], "label": region["label"]} for region in facets.get("region", [])]


def fetch_cities(session: requests.Session, region_id: int) -> list[dict]:
    """Fetch the cities/municipalities within one district.

    Cities only surface as a facet once a search is filtered down to a single
    region, so resolving them means re-running a generic search with that
    region applied and reading `metaData.facets.city` off the result.
    """
    html = fetch_page("", 1, session, extra_params={"search[region_id]": region_id})
    state = extract_prerendered_state(html)
    facets = state["listing"]["listing"]["metaData"].get("facets") or {}
    return [{"id": city["id"], "label": city["label"]} for city in facets.get("city", [])]


def extract_prerendered_state(html: str) -> dict:
    """Parse the JSON data OLX embeds in `window.__PRERENDERED_STATE__ = "..."`.

    That assignment's right-hand side is a JSON *document* that has itself been
    JSON-encoded as a string, so it can safely contain quotes/newlines/unicode
    without breaking out of the surrounding <script> tag. Escaped quotes (`\\"`)
    inside it look identical to the real terminating quote, so a naive search
    for the next `"` (or a non-greedy regex) stops too early. Instead we scan
    character-by-character, skipping `\\X` escape pairs, until an unescaped `"`.
    Then `json.loads` runs twice: once to undo the string's own JSON-escaping
    (yielding the actual JSON text), and again to parse that text.
    """
    try:
        start = html.index(PRERENDERED_STATE_MARKER) + len(PRERENDERED_STATE_MARKER)
    except ValueError:
        raise ValueError(
            "OLX page format changed — __PRERENDERED_STATE__ marker not found. "
            "Fetch the search URL with curl and grep for __PRERENDERED_STATE__ to verify."
        )
    end = start
    while end < len(html):
        char = html[end]
        if char == "\\":
            end += 2
            continue
        if char == '"':
            break
        end += 1
    else:
        raise ValueError("OLX page format changed — __PRERENDERED_STATE__ payload has no closing quote (truncated response?)")

    escaped_json = html[start:end]
    json_text = json.loads(f'"{escaped_json}"')
    return json.loads(json_text)


def parse_listings(html: str) -> list[Listing]:
    state = extract_prerendered_state(html)
    listings = []
    for ad in state["listing"]["listing"]["ads"]:
        location = ad.get("location") or {}
        photos = ad.get("photos") or []
        listings.append(
            Listing(
                id=str(ad["id"]),
                title=ad["title"],
                price=ad.get("price", {}).get("displayValue", ""),
                city=location.get("cityName", ""),
                district=location.get("regionName", ""),
                date=ad["createdTime"][:10],
                url=ad["url"],
                image_url=photos[0] if photos else None,
            )
        )
    return listings

def search(
    query: str,
    max_pages: int = 1,
    delay: float = 1.0,
    *,
    category: str | None = None,
    category_path: str | None = None,
    region_id: int | None = None,
    city_id: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    condition: str | None = None,
    sort: str | None = None,
) -> list[Listing]:
    """Scrape up to `max_pages` pages of OLX.pt search results for `query`.

    `category` accepts a partial name/path and resolves it via `find_category`
    (raises `ValueError` if missing or ambiguous — makes an extra HTTP request).
    `category_path` accepts an already-resolved OLX path slug directly, skipping
    that extra fetch — pass this when the caller (e.g. the GUI's `CategoryPicker`)
    already holds the resolved path. `region_id`/`city_id` are OLX's own
    district/city ids (see `fetch_regions`/`fetch_cities`). `sort` must be one
    of `SORT_OPTIONS` (raises `ValueError` for unknown values).

    Stops early once a page yields no new listings (end of results reached).
    """
    session = requests.Session()

    if category and not category_path:
        category_path = find_category(fetch_categories(session), category)["path"]

    extra_params: dict[str, object] = {}
    if region_id is not None:
        extra_params["search[region_id]"] = region_id
    if city_id is not None:
        extra_params["search[city_id]"] = city_id
    if min_price is not None:
        extra_params["search[filter_float_price:from]"] = min_price
    if max_price is not None:
        extra_params["search[filter_float_price:to]"] = max_price
    if condition:
        extra_params["search[filter_enum_state][0]"] = condition
    if sort:
        if sort not in SORT_OPTIONS:
            raise ValueError(f"Unknown sort {sort!r} — must be one of: {', '.join(SORT_OPTIONS)}")
        extra_params["search[order]"] = SORT_OPTIONS[sort]

    results: list[Listing] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        html = fetch_page(query, page, session, category_path=category_path, extra_params=extra_params)
        new_listings = [l for l in parse_listings(html) if l.id not in seen_ids]
        if not new_listings:
            break

        seen_ids.update(l.id for l in new_listings)
        results.extend(new_listings)

        if page < max_pages:
            time.sleep(delay)

    return results
