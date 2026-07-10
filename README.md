# OLX Viewer

A Python desktop application for searching, combining, saving, and analysing listings from [OLX Portugal](https://www.olx.pt/).

OLX Viewer provides a visual interface for browsing adverts, grouping multiple searches into reusable item profiles, hiding irrelevant results, tracking saved listings, and viewing price history over time.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/GUI-PySide6-green)
![SQLite](https://img.shields.io/badge/Database-SQLite-lightgrey)

## Features

* Search OLX.pt from a desktop interface
* Filter using OLX's own categories, district/cities, page count, and sort order
* Display results as image cards
* Save, open or hide listings
* Track when saved listings were first and last seen
* Save individual search configurations
* Group several searches into named **items**
* Combine results while automatically removing duplicates
* Record price snapshots
* View price-history charts and statistics
* Export results to CSV or JSON
* Open JSON results in a sortable HTML viewer

## Requirements

* Python 3.10 or newer
* Internet access
* Windows, Linux, or macOS

Main dependencies:

* `requests`
* `PySide6`
* `matplotlib`

## Installation

Clone the repository:

```bash
git clone https://github.com/kxrvvs/olx-viewer.git
cd olx-viewer
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it.

**Windows**

```powershell
.venv\Scripts\activate
```

**Linux/macOS**

```bash
source .venv/bin/activate
```

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

## Run the desktop app

```bash
python -m olx_scraper.gui
```

The app stores saved searches, listings, hidden adverts, grouped items, and price snapshots in:

```text
olx_listings.db
```

## Desktop workflow

### Search

Enter a query, select the number of pages and any category or location filters, then run the search.

Results can be:

* Opened in the browser
* Selected individually or as a range
* Saved to the local database
* Hidden from future searches
* Re-sorted locally by price or date

### Items and combined searches

An **item** is a named group containing one or more search configurations.

For example, an item called `ThinkPad T480` could contain:

* `ThinkPad T480`
* `Lenovo T480`
* `T480 i5`

Running the item executes every configured search and combines the results into one deduplicated list.

Each run can also create price snapshots for the analytics view.

### Analytics

The analytics window uses stored search runs to display price history and statistics for an item. This makes it possible to compare available listings and observe market changes between searches.

### Database and hidden adverts

Saved listings include:

* Title
* Price
* Location
* Publication date
* First-seen date
* Last-seen date

Hidden adverts are stored separately and automatically excluded from later searches. They can be restored from the hidden-adverts view.

## Command-line export

Export listings to CSV:

```bash
python -m olx_scraper.cli "iphone 13" --pages 3 --output results.csv
```

Export listings to JSON:

```bash
python -m olx_scraper.cli "iphone 13" --pages 3 --output results.json
```

Available options:

```text
-p, --pages       Number of result pages
-o, --output      Output file ending in .csv or .json
--delay           Delay between requests in seconds
--category        OLX category name or path
--min-price       Minimum price
--max-price       Maximum price
--condition       new or used
--sort            OLX result ordering
```

Show the full command reference:

```bash
python -m olx_scraper.cli --help
```

## HTML viewer

Open an existing JSON export as a sortable card grid:

```bash
python -m olx_scraper.view results.json
```

Run a new search before opening the viewer:

```bash
python -m olx_scraper.view results.json --query "iphone 13" --pages 3
```

## Project structure

```text
olx_scraper/
├── cli.py       # Command-line interface
├── db.py        # SQLite storage and price-history data
├── export.py    # CSV and JSON export
├── gui.py       # PySide6 desktop application
├── scraper.py   # Requests, parsing, filters, and listing model
└── view.py      # Standalone HTML results viewer
```

## How it works

OLX Viewer requests OLX search-result pages and extracts the listing data embedded in their initial HTML.

It does not require Selenium, a headless browser, an OLX account, or API credentials.

Searches use a shared HTTP session, remove duplicate listing IDs, and include a delay between page requests.

## Limitations

* The project currently targets `olx.pt`.
* It depends on OLX's current page structure.
* Website changes may require updates to the scraper.
* Search results are limited to data exposed by OLX.
* No automated test suite is currently included.

## Disclaimer

This is an unofficial project and is not affiliated with, endorsed by, or maintained by OLX.

Use it responsibly, avoid excessive request rates, and ensure that your use complies with OLX's applicable terms and policies.
::: 
