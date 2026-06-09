from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import requests

try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    _MATPLOTLIB_OK = True
except ImportError:
    _MATPLOTLIB_OK = False

from PySide6.QtCore import QObject, QSize, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    # QAbstractSpinBox / QDoubleSpinBox: only used by the price-range filter,
    # which is commented out in `_build_search_bar` (not needed for now).
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import db
from .scraper import SORT_OPTIONS, Listing, fetch_categories, fetch_cities, fetch_regions, numeric_price, search

CARD_WIDTH = 220
IMAGE_HEIGHT = 140
DB_PATH = Path(__file__).parent.parent / "olx_listings.db"

_SAFE_URL_SCHEMES = ("https://", "http://", "/")


def _open_url_safe(url: str) -> None:
    """Open `url` in the system browser, refusing non-http(s) schemes.

    OLX's ad data is verbatim from their API and could theoretically contain
    javascript: or file: URLs; validating the scheme prevents those from
    executing on double-click.
    """
    if url and any(url.startswith(scheme) for scheme in _SAFE_URL_SCHEMES):
        webbrowser.open(url)

# Friendly labels for the *local* re-sort dropdown — distinct from `SORT_OPTIONS`,
# which drives OLX's own server-side ordering. This one re-orders whatever is
# already on screen, instantly, without re-scraping (mirrors the HTML viewer's
# client-side sort).
LOCAL_SORT_OPTIONS = [
    ("Default order", None),
    ("Price: Low to High", "price-asc"),
    ("Price: High to Low", "price-desc"),
    ("Date: Oldest First", "date-asc"),
    ("Date: Newest First", "date-desc"),
]


class ScrapeWorker(QObject):
    """Runs `search()` off the GUI thread so the window stays responsive."""

    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, kwargs: dict):
        super().__init__()
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            results = search(**self._kwargs)
        except ValueError as exc:
            self.failed.emit(str(exc))
            return
        except requests.RequestException as exc:
            self.failed.emit(f"Network error while scraping: {exc}")
            return
        except Exception as exc:
            self.failed.emit(f"Unexpected error while scraping: {exc}")
            return
        self.finished.emit(results)


class CategoryLoader(QObject):
    """Fetches OLX's category tree once, off the GUI thread (it's a real HTTP request)."""

    loaded = Signal(dict)
    failed = Signal(str)

    def run(self) -> None:
        try:
            categories = fetch_categories(requests.Session())
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.loaded.emit(categories)


class RegionLoader(QObject):
    """Fetches OLX's district list once, off the GUI thread — mirrors `CategoryLoader`."""

    loaded = Signal(list)
    failed = Signal(str)

    def run(self) -> None:
        try:
            regions = fetch_regions(requests.Session())
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.loaded.emit(regions)


class CityLoader(QObject):
    """Fetches the cities within one district, off the GUI thread.

    Unlike `CategoryLoader`/`RegionLoader` (each fetched once up front), this
    one is spun up fresh every time `LocationPicker` gets a new district —
    cities only surface once a search is filtered to a single region (see
    `fetch_cities`), so there's no whole-country list to pre-fetch. The
    `region_id` rides along on both signals so a picker that's since moved on
    to a different district can recognise and discard a stale answer.
    """

    loaded = Signal(int, list)
    failed = Signal(int, str)

    def __init__(self, region_id: int):
        super().__init__()
        self.region_id = region_id

    def run(self) -> None:
        try:
            cities = fetch_cities(requests.Session(), self.region_id)
        except Exception as exc:
            self.failed.emit(self.region_id, str(exc))
            return
        self.loaded.emit(self.region_id, cities)


class MultiScrapeWorker(QObject):
    """Runs a list of `search()` calls in sequence and merges the results.

    Each individual search's results are deduplicated against all previous ones
    by listing id, so the combined output contains no duplicates even when
    searches overlap. Emits `progress` before each search so the status bar
    can show which search is running.
    """

    progress = Signal(int, int, str)  # current index (1-based), total, query
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, searches: list[dict]):
        super().__init__()
        self._searches = searches

    def run(self) -> None:
        results: list = []
        seen_ids: set[str] = set()
        total = len(self._searches)
        for i, kwargs in enumerate(self._searches):
            self.progress.emit(i + 1, total, kwargs["query"])
            try:
                listings = search(**kwargs)
            except Exception as exc:
                self.failed.emit(
                    f"Search {i + 1}/{total} ({kwargs['query']!r}) failed: {exc}"
                )
                return
            for listing in listings:
                if listing.id not in seen_ids:
                    seen_ids.add(listing.id)
                    results.append(listing)
        self.finished.emit(results)


class CategoryPicker(QWidget):
    """Three always-visible dropdowns: category, subcategory, sub-subcategory.

    All three are shown up front so the layout never jumps around. The second
    and third start out greyed out; each one lights up — populated with that
    level's children — only once you've picked something one level up that
    actually *has* children to drill into.
    """

    LEVEL_LABELS = ["Any category", "Any subcategory", "Any sub-subcategory"]

    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.categories: dict[str, dict] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.combos: list[QComboBox] = []
        for label in self.LEVEL_LABELS:
            combo = QComboBox()
            combo.addItem(label, None)
            combo.setEnabled(False)
            layout.addWidget(combo, 1)
            self.combos.append(combo)

        for level, combo in enumerate(self.combos):
            combo.currentIndexChanged.connect(lambda _index, lvl=level: self._on_changed(lvl))

    def set_categories(self, categories: dict[str, dict]) -> None:
        self.categories = categories
        self._fill(0, parent=None)

    def show_error(self, message: str) -> None:
        for combo in self.combos:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(message, None)
            combo.setEnabled(False)
            combo.blockSignals(False)

    def selected_category(self) -> dict | None:
        """The most specific category picked, checking sub-sub before sub before top."""
        for combo in reversed(self.combos):
            category = combo.currentData()
            if category is not None:
                return category
        return None

    def selected_path(self) -> str | None:
        category = self.selected_category()
        return category["path"] if category else None

    def select_by_path(self, category_path: str | None) -> None:
        """Programmatically reproduce a category selection from a saved `path`
        (used to restore filters when re-running a saved search). Walks the
        `parentId` chain up to the root to rebuild the lineage, then drives
        each combo's selection top-down — `_on_changed` fires naturally and
        populates the next level as it goes, just like a user clicking through.
        """
        if not self.categories:
            return

        # Reset to "Any category" without disturbing combo[0]'s populated
        # root-category list — `_reset_below(0)` only clears levels 1+.
        self.combos[0].blockSignals(True)
        self.combos[0].setCurrentIndex(0)
        self.combos[0].blockSignals(False)
        self._reset_below(0)

        if not category_path:
            return

        target = next((c for c in self.categories.values() if c["path"] == category_path), None)
        if target is None:
            return

        chain = []
        node = target
        while node is not None:
            chain.append(node)
            parent_id = node.get("parentId")
            node = self.categories.get(str(parent_id)) if parent_id else None
        chain.reverse()

        for level, category in enumerate(chain):
            if level >= len(self.combos):
                break
            combo = self.combos[level]
            index = combo.findData(category)
            if index == -1:
                break
            combo.setCurrentIndex(index)

    def _children_of(self, category: dict | None) -> list[dict]:
        if category is None:
            ids = [c["id"] for c in self.categories.values() if not c.get("parentId")]
        else:
            ids = category.get("children") or []
        children = [self.categories[str(cid)] for cid in ids if str(cid) in self.categories]
        return sorted(children, key=lambda c: c.get("displayOrder", 0))

    def _fill(self, level: int, parent: dict | None) -> None:
        combo = self.combos[level]
        entries = self._children_of(parent)

        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.LEVEL_LABELS[level], None)
        for entry in entries:
            combo.addItem(entry["name"], entry)
        combo.setEnabled(bool(entries))
        combo.blockSignals(False)

    def _reset_below(self, level: int) -> None:
        for deeper in range(level + 1, len(self.combos)):
            combo = self.combos[deeper]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self.LEVEL_LABELS[deeper], None)
            combo.setEnabled(False)
            combo.blockSignals(False)

    def _on_changed(self, level: int) -> None:
        category = self.combos[level].currentData()
        self._reset_below(level)
        if category is not None and level + 1 < len(self.combos):
            self._fill(level + 1, parent=category)
        self.selection_changed.emit()


class LocationPicker(QWidget):
    """Two cascading dropdowns: district, then city.

    Mirrors `CategoryPicker`'s always-visible-combos layout, but the data
    behind it can't be pre-fetched as a whole tree the way categories can —
    OLX only exposes districts ("regions") as a facet on a generic search, and
    cities only show up as a facet once a district is chosen. So the district
    list is loaded once up front (by `RegionLoader`, handed to `set_regions`)
    while the city list is fetched fresh — in the background, via
    `city_lookup_requested` — every time the district selection changes.
    """

    LEVEL_LABELS = ["Any district", "Any city"]

    selection_changed = Signal()
    city_lookup_requested = Signal(int)

    def __init__(self):
        super().__init__()
        self.regions: list[dict] = []
        self._pending_city_id: int | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.district_combo = QComboBox()
        self.district_combo.addItem(self.LEVEL_LABELS[0], None)
        self.district_combo.setEnabled(False)
        layout.addWidget(self.district_combo, 1)

        self.city_combo = QComboBox()
        self.city_combo.addItem(self.LEVEL_LABELS[1], None)
        self.city_combo.setEnabled(False)
        layout.addWidget(self.city_combo, 1)

        self.district_combo.currentIndexChanged.connect(self._on_district_changed)
        self.city_combo.currentIndexChanged.connect(lambda _index: self.selection_changed.emit())

    def set_regions(self, regions: list[dict]) -> None:
        self.regions = sorted(regions, key=lambda region: region["label"])
        self.district_combo.blockSignals(True)
        self.district_combo.clear()
        self.district_combo.addItem(self.LEVEL_LABELS[0], None)
        for region in self.regions:
            self.district_combo.addItem(region["label"], region)
        self.district_combo.setEnabled(True)
        self.district_combo.blockSignals(False)

    def show_error(self, message: str) -> None:
        self.district_combo.blockSignals(True)
        self.district_combo.clear()
        self.district_combo.addItem(message, None)
        self.district_combo.setEnabled(False)
        self.district_combo.blockSignals(False)
        self._reset_city_combo()

    def selected_region(self) -> dict | None:
        return self.district_combo.currentData()

    def selected_city(self) -> dict | None:
        return self.city_combo.currentData()

    def select_by_ids(self, region_id: int | None, city_id: int | None) -> None:
        """Programmatically reproduce a district/city selection from a saved
        search's stored ids (used to restore filters when re-running one).
        Picking the district drives `_on_district_changed` exactly as a click
        would — kicking off the usual async city fetch — and `_pending_city_id`
        records which city to land on once that batch arrives in `set_cities`.
        """
        self._pending_city_id = city_id

        if region_id is None:
            self.district_combo.setCurrentIndex(0)
            return

        index = next(
            (i for i in range(self.district_combo.count())
             if (data := self.district_combo.itemData(i)) is not None and data["id"] == region_id),
            -1,
        )
        if index == -1:
            self._pending_city_id = None
            return
        self.district_combo.setCurrentIndex(index)

    def set_cities(self, region_id: int, cities: list[dict]) -> None:
        """Populate the city dropdown with a freshly-fetched batch — ignored if
        the user has since picked a different district than this batch is for."""
        current = self.selected_region()
        if current is None or current["id"] != region_id:
            return

        ordered = sorted(cities, key=lambda city: city["label"])
        pending = self._pending_city_id
        self._pending_city_id = None

        self.city_combo.blockSignals(True)
        self.city_combo.clear()
        self.city_combo.addItem(self.LEVEL_LABELS[1], None)
        for city in ordered:
            self.city_combo.addItem(city["label"], city)
        self.city_combo.setEnabled(bool(ordered))

        if pending is not None:
            index = next((i for i in range(self.city_combo.count())
                          if (data := self.city_combo.itemData(i)) is not None and data["id"] == pending), -1)
            if index != -1:
                self.city_combo.setCurrentIndex(index)
        self.city_combo.blockSignals(False)

    def city_lookup_failed(self, region_id: int) -> None:
        current = self.selected_region()
        if current is not None and current["id"] == region_id:
            self._pending_city_id = None
            self._reset_city_combo("Cities unavailable")

    def _reset_city_combo(self, label: str | None = None) -> None:
        self.city_combo.blockSignals(True)
        self.city_combo.clear()
        self.city_combo.addItem(label or self.LEVEL_LABELS[1], None)
        self.city_combo.setEnabled(False)
        self.city_combo.blockSignals(False)

    def _on_district_changed(self, _index: int) -> None:
        region = self.selected_region()
        self._reset_city_combo()
        if region is not None:
            self.city_lookup_requested.emit(region["id"])
        else:
            self._pending_city_id = None
        self.selection_changed.emit()


def cover_pixmap(pixmap: QPixmap, width: int, height: int) -> QPixmap:
    """Scale + center-crop a pixmap to exactly `width`x`height` (like CSS `object-fit: cover`)."""
    scaled = pixmap.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, (scaled.height() - height) // 2)
    return scaled.copy(x, y, width, height)


class ListingCard(QFrame):
    """A clickable card showing one listing's image, price, district and date.

    A single click toggles selection; Shift+click selects a range (MainWindow
    handles this via the `clicked` signal). Double-click or the 🔗 badge opens
    the listing's URL. Cards created with `is_hidden=True` are read-only (shown
    faded when the "Show hidden ads" toggle is on) and never emit `clicked`."""

    clicked = Signal(object, bool)  # (self, shift_held)

    def __init__(self, listing: Listing, *, is_hidden: bool = False):
        super().__init__()
        self.listing = listing
        self._is_hidden = is_hidden

        self.setFixedWidth(CARD_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(listing.title)
        if is_hidden:
            self.setStyleSheet(
                "ListingCard { background: #e0e0e0; border-radius: 8px; }"
            )
        else:
            self.setStyleSheet(
                "ListingCard { background: white; border-radius: 8px; }"
                "ListingCard:hover { background: #eef3fb; }"
            )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(2)

        self.image_label = QLabel("No pic")
        self.image_label.setFixedSize(CARD_WIDTH, IMAGE_HEIGHT)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(
            "background: #ddd; color: #888; font-size: 13px;"
            "border-top-left-radius: 8px; border-top-right-radius: 8px;"
        )
        layout.addWidget(self.image_label)

        self.select_box = QCheckBox(self.image_label)
        self.select_box.setCursor(Qt.CursorShape.ArrowCursor)
        self.select_box.setStyleSheet(
            "QCheckBox { background: rgba(255, 255, 255, 0.85); border-radius: 4px; padding: 2px; }"
            "QCheckBox::indicator { width: 18px; height: 18px; }"
        )
        self.select_box.setEnabled(not is_hidden)
        self.select_box.adjustSize()
        self.select_box.move(CARD_WIDTH - self.select_box.width() - 6, 6)
        self.select_box.raise_()

        # The price gets its own corner badge too — overlaid on the bottom-right
        # of the image like a price tag, rather than taking a layout row below it.
        self.price_label = QLabel(listing.price, self.image_label)
        self.price_label.setStyleSheet(
            "QLabel {"
            "  background: rgba(0, 0, 0, 0.65); color: white; font-weight: bold;"
            "  border-radius: 4px; padding: 3px 7px;"
            "}"
        )
        self.price_label.adjustSize()
        self.price_label.move(
            CARD_WIDTH - self.price_label.width() - 6,
            IMAGE_HEIGHT - self.price_label.height() - 6,
        )
        self.price_label.raise_()

        # A bottom-left counterpart to the price tag — a quick one-click way
        # to open the listing without double-clicking the card itself.
        self.open_link_button = QPushButton("🔗", self.image_label)
        self.open_link_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_link_button.setToolTip("Open this listing in your browser")
        self.open_link_button.setFlat(True)
        self.open_link_button.setStyleSheet(
            "QPushButton {"
            "  background: rgba(0, 0, 0, 0.65); color: white; border: none;"
            "  border-radius: 4px; padding: 3px 7px; font-size: 13px;"
            "}"
            "QPushButton:hover { background: rgba(0, 0, 0, 0.8); }"
        )
        self.open_link_button.adjustSize()
        self.open_link_button.move(6, IMAGE_HEIGHT - self.open_link_button.height() - 6)
        self.open_link_button.raise_()
        self.open_link_button.clicked.connect(self._open_url)

        details_row = QHBoxLayout()
        details_row.setContentsMargins(8, 4, 8, 0)

        self.district_label = QLabel(listing.district)
        self.district_label.setStyleSheet("color: #666; font-size: 12px;")
        details_row.addWidget(self.district_label)

        details_row.addStretch()

        self.date_label = QLabel(listing.date)
        self.date_label.setStyleSheet("color: #666; font-size: 12px;")
        details_row.addWidget(self.date_label)

        layout.addLayout(details_row)

    def set_image(self, pixmap: QPixmap) -> None:
        self.image_label.setPixmap(pixmap)
        self.image_label.setText("")

    def is_selected(self) -> bool:
        return self.select_box.isChecked()

    def _open_url(self) -> None:
        _open_url_safe(self.listing.url)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._is_hidden:
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if not shift:
                self.select_box.setChecked(not self.select_box.isChecked())
            self.clicked.emit(self, shift)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_url()
        super().mouseDoubleClickEvent(event)


class ItemRow(QFrame):
    """One row in the Home tab's Items list.

    Shows the item name and a one-line summary of its searches, plus Run
    (fire all searches and show combined results on the Search tab), Edit
    (rename or remove individual searches), and Delete buttons.
    """

    run_requested = Signal(object)
    analytics_requested = Signal(object)
    edit_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, row):
        super().__init__()
        self.row = row
        self.setToolTip("Double-click to run this item")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        name_label = QLabel(row["name"])
        name_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(name_label, 1)

        run_btn = QPushButton("Run")
        run_btn.setToolTip("Run all searches in this item and show combined results")
        run_btn.clicked.connect(lambda: self.run_requested.emit(self.row))
        layout.addWidget(run_btn)

        analytics_btn = QPushButton("Analytics")
        analytics_btn.setToolTip("View price history and statistics for this item")
        analytics_btn.clicked.connect(lambda: self.analytics_requested.emit(self.row))
        layout.addWidget(analytics_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.setToolTip("Rename this item or remove individual searches from it")
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.row))
        layout.addWidget(edit_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.setToolTip("Permanently delete this item and all its searches")
        delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.row))
        layout.addWidget(delete_btn)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.run_requested.emit(self.row)
        super().mouseDoubleClickEvent(event)


class ItemEditDialog(QDialog):
    """Edit an item: rename it, manage its searches, and add new ones.

    Contains a scrollable list of existing searches (each with Edit/Remove
    buttons) and a filter form below for adding new searches or editing
    existing ones — so the user never has to leave the dialog to configure
    a search.
    """

    def __init__(
        self,
        item_row,
        searches,
        conn,
        *,
        categories: dict[str, dict] | None = None,
        regions: list[dict] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Edit item — {item_row['name']}")
        self.conn = conn
        self.item_id = item_row["id"]
        self._searches = list(searches)
        self._editing_search_id: int | None = None
        self._city_loaders: list[tuple[QThread, CityLoader]] = []
        self._pending_run_search = None

        layout = QVBoxLayout(self)

        # --- Name ---
        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(item_row["name"])
        rename_row.addWidget(self.name_edit, 1)
        layout.addLayout(rename_row)

        # --- Existing searches ---
        layout.addWidget(QLabel("Searches:"))
        self._search_list_widget = QWidget()
        self._search_list_layout = QVBoxLayout(self._search_list_widget)
        self._search_list_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidget(self._search_list_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(160)
        layout.addWidget(scroll)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #ccc;")
        layout.addWidget(sep)

        # --- Add / Edit search form ---
        self._form_label = QLabel("Add search:")
        self._form_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._form_label)

        query_row = QHBoxLayout()
        query_row.addWidget(QLabel("Query:"))
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("e.g. iphone 13")
        query_row.addWidget(self.query_edit, 1)
        query_row.addWidget(QLabel("Pages:"))
        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(1, 50)
        self.pages_spin.setValue(1)
        query_row.addWidget(self.pages_spin)
        layout.addLayout(query_row)

        self.category_picker = CategoryPicker()
        if categories:
            self.category_picker.set_categories(categories)
        layout.addWidget(self.category_picker)

        self.location_picker = LocationPicker()
        if regions:
            self.location_picker.set_regions(regions)
        self.location_picker.city_lookup_requested.connect(self._start_city_loading)
        layout.addWidget(self.location_picker)

        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("OLX default order", None)
        for key in sorted(SORT_OPTIONS):
            self.sort_combo.addItem(key, key)
        idx = self.sort_combo.findData("newest")
        if idx != -1:
            self.sort_combo.setCurrentIndex(idx)
        sort_row.addWidget(self.sort_combo)
        sort_row.addStretch()
        layout.addLayout(sort_row)

        form_btn_row = QHBoxLayout()
        self._cancel_edit_btn = QPushButton("Cancel edit")
        self._cancel_edit_btn.setVisible(False)
        self._cancel_edit_btn.clicked.connect(self._cancel_edit)
        form_btn_row.addWidget(self._cancel_edit_btn)
        form_btn_row.addStretch()
        self._add_btn = QPushButton("Add search")
        self._add_btn.clicked.connect(self._add_or_update_search)
        form_btn_row.addWidget(self._add_btn)
        layout.addLayout(form_btn_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #ccc;")
        layout.addWidget(sep2)

        dlg_btn_row = QHBoxLayout()
        dlg_btn_row.addStretch()
        done_btn = QPushButton("Done")
        done_btn.clicked.connect(self.accept)
        dlg_btn_row.addWidget(done_btn)
        layout.addLayout(dlg_btn_row)

        self.setMinimumWidth(560)
        self._rebuild_search_list()

    # -- City loading (mirrors MainWindow._start_city_loading) -------------

    def _start_city_loading(self, region_id: int) -> None:
        thread = QThread(self)
        loader = CityLoader(region_id)
        loader.moveToThread(thread)
        thread.started.connect(loader.run)
        loader.loaded.connect(self._on_cities_loaded)
        loader.failed.connect(self._on_cities_failed)
        loader.loaded.connect(thread.quit)
        loader.failed.connect(thread.quit)
        thread.finished.connect(lambda: self._cleanup_city_loader(thread, loader))
        self._city_loaders.append((thread, loader))
        thread.start()

    def _on_cities_loaded(self, region_id: int, cities: list) -> None:
        self.location_picker.set_cities(region_id, cities)

    def _on_cities_failed(self, region_id: int, _: str) -> None:
        self.location_picker.city_lookup_failed(region_id)

    def _cleanup_city_loader(self, thread: QThread, loader: CityLoader) -> None:
        if (thread, loader) in self._city_loaders:
            self._city_loaders.remove((thread, loader))
        thread.deleteLater()
        loader.deleteLater()

    # -- Search list -------------------------------------------------------

    def _rebuild_search_list(self) -> None:
        while self._search_list_layout.count():
            item = self._search_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._searches:
            self._search_list_layout.addWidget(QLabel("No searches yet — add one below."))
            return

        for s in self._searches:
            row_widget = QFrame()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 2, 0, 2)
            parts = [f'"{s["query"]}"']
            if s["category_label"]:
                parts.append(s["category_label"])
            loc = " / ".join(l for l in (s["region_label"], s["city_label"]) if l)
            if loc:
                parts.append(loc)
            if s["sort"]:
                parts.append(s["sort"])
            parts.append(f'{s["pages"]} page(s)')
            label = QLabel(" · ".join(parts))
            if self._editing_search_id == s["id"]:
                label.setStyleSheet("color: #0066cc;")
            row_layout.addWidget(label, 1)
            run_btn = QPushButton("Run")
            run_btn.setToolTip("Run this search and show results on the Search tab")
            run_btn.clicked.connect(lambda _, s=s: self._run_search_and_close(s))
            row_layout.addWidget(run_btn)
            edit_btn = QPushButton("Edit")
            edit_btn.clicked.connect(lambda _, s=s: self._start_edit(s))
            row_layout.addWidget(edit_btn)
            rm_btn = QPushButton("Remove")
            rm_btn.clicked.connect(lambda _, s=s: self._remove_search(s))
            row_layout.addWidget(rm_btn)
            self._search_list_layout.addWidget(row_widget)

        self._search_list_layout.addStretch()

    def _start_edit(self, s) -> None:
        self._editing_search_id = s["id"]
        self._form_label.setText("Edit search:")
        self._add_btn.setText("Update search")
        self._cancel_edit_btn.setVisible(True)
        self.query_edit.setText(s["query"])
        self.pages_spin.setValue(s["pages"])
        idx = self.sort_combo.findData(s["sort"])
        self.sort_combo.setCurrentIndex(idx if idx != -1 else 0)
        self.category_picker.select_by_path(s["category_path"])
        self.location_picker.select_by_ids(s["region_id"], s["city_id"])
        self._rebuild_search_list()

    def _cancel_edit(self) -> None:
        self._editing_search_id = None
        self._form_label.setText("Add search:")
        self._add_btn.setText("Add search")
        self._cancel_edit_btn.setVisible(False)
        self.query_edit.clear()
        self.pages_spin.setValue(1)
        idx = self.sort_combo.findData("newest")
        self.sort_combo.setCurrentIndex(idx if idx != -1 else 0)
        self.category_picker.select_by_path(None)
        self.location_picker.select_by_ids(None, None)
        self._rebuild_search_list()

    def _add_or_update_search(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            return

        category = self.category_picker.selected_category()
        region = self.location_picker.selected_region()
        city = self.location_picker.selected_city()
        sort = self.sort_combo.currentData()

        if self._editing_search_id is not None:
            db.update_item_search(
                self.conn,
                self._editing_search_id,
                query=query,
                pages=self.pages_spin.value(),
                category_path=category["path"] if category else None,
                category_label=category["name"] if category else None,
                region_id=region["id"] if region else None,
                region_label=region["label"] if region else None,
                city_id=city["id"] if city else None,
                city_label=city["label"] if city else None,
                sort=sort,
            )
        else:
            db.add_item_search(
                self.conn,
                self.item_id,
                query=query,
                pages=self.pages_spin.value(),
                category_path=category["path"] if category else None,
                category_label=category["name"] if category else None,
                region_id=region["id"] if region else None,
                region_label=region["label"] if region else None,
                city_id=city["id"] if city else None,
                city_label=city["label"] if city else None,
                sort=sort,
            )

        self._editing_search_id = None
        self._form_label.setText("Add search:")
        self._add_btn.setText("Add search")
        self._cancel_edit_btn.setVisible(False)
        self.query_edit.clear()
        self.pages_spin.setValue(1)
        idx = self.sort_combo.findData("newest")
        self.sort_combo.setCurrentIndex(idx if idx != -1 else 0)
        self.category_picker.select_by_path(None)
        self.location_picker.select_by_ids(None, None)
        self._searches = list(db.fetch_item_searches(self.conn, self.item_id))
        self._rebuild_search_list()

    def _run_search_and_close(self, s) -> None:
        self._pending_run_search = s
        self.accept()

    def _remove_search(self, s) -> None:
        if self._editing_search_id == s["id"]:
            self._cancel_edit()
        db.delete_item_search(self.conn, s["id"])
        self._searches = [x for x in self._searches if x["id"] != s["id"]]
        self._rebuild_search_list()

    def _stop_city_loaders(self) -> None:
        for thread, _ in self._city_loaders:
            thread.quit()
            thread.wait()
        self._city_loaders.clear()

    def accept(self) -> None:
        self._stop_city_loaders()
        name = self.name_edit.text().strip()
        if name:
            db.rename_item(self.conn, self.item_id, name)
        super().accept()

    def reject(self) -> None:
        self._stop_city_loaders()
        super().reject()


class _SortableItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by a numeric UserRole value rather than display text.

    None values sort to the end regardless of sort direction (treated as
    infinity in ascending, negative-infinity in descending).
    """
    def __lt__(self, other: QTableWidgetItem) -> bool:
        a = self.data(Qt.ItemDataRole.UserRole)
        b = other.data(Qt.ItemDataRole.UserRole)
        if a is None and b is None:
            return False
        if a is None:
            return False
        if b is None:
            return True
        return a < b


class ItemAnalyticsDialog(QDialog):
    """Price history charts and statistics for one item."""

    def __init__(self, item_name: str, item_id: int, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle(f"Analytics — {item_name}")
        self.setMinimumSize(860, 580)
        self.resize(980, 660)

        # Mutable chart / table state
        self._listings_table: QTableWidget | None = None
        self._rows_data: list[dict] = []
        self._trend_canvas = None
        self._trend_ax = None
        self._trend_annotation = None
        self._trend_runs: list = []
        self._dist_canvas = None
        self._dist_ax = None
        self._hist_patches: list = []
        self._hist_bins: list[float] = []
        self._active_bin: int | None = None

        layout = QVBoxLayout(self)

        runs = db.fetch_price_runs(conn, item_id)
        listing_history = db.fetch_listing_history(conn, item_id)

        if not runs:
            msg = QLabel(
                "No price history yet.\n\n"
                "Run this item from the Home tab or Search tab — "
                "each run records a price snapshot for every listing found."
            )
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setStyleSheet("color: #666; font-size: 13px; padding: 40px;")
            layout.addWidget(msg, 1)
            close_row = QHBoxLayout()
            close_row.addStretch()
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.accept)
            close_row.addWidget(close_btn)
            layout.addLayout(close_row)
            return

        # --- Summary stats ---
        all_avgs = [r["avg_price"] for r in runs if r["avg_price"] is not None]
        all_mins = [r["min_price"] for r in runs if r["min_price"] is not None]
        all_maxs = [r["max_price"] for r in runs if r["max_price"] is not None]
        total_snapshots = sum(r["listing_count"] for r in runs)
        unique_listings = len({row["listing_id"] for row in listing_history})

        stat_parts = [
            f'{len(runs)} session{"s" if len(runs) != 1 else ""}',
            f'{total_snapshots} snapshots',
            f'{unique_listings} unique listings',
        ]
        if all_avgs:
            overall_avg = sum(all_avgs) / len(all_avgs)
            if all_mins and all_maxs:
                stat_parts.append(f'Range: {min(all_mins):.0f}€ – {max(all_maxs):.0f}€')
            stat_parts.append(f'Avg: {overall_avg:.0f}€')

        stats_label = QLabel('  ·  '.join(stat_parts))
        stats_label.setStyleSheet("font-size: 13px; padding: 4px 0;")
        layout.addWidget(stats_label)

        # --- Charts ---
        if _MATPLOTLIB_OK:
            charts_widget = QWidget()
            charts_layout = QHBoxLayout(charts_widget)
            charts_layout.setContentsMargins(0, 0, 0, 0)

            fig1 = Figure(figsize=(6, 3.5))
            self._trend_canvas = FigureCanvasQTAgg(fig1)
            self._trend_ax = fig1.add_subplot(111)
            self._setup_price_trend(self._trend_ax, runs)
            fig1.tight_layout()
            charts_layout.addWidget(self._trend_canvas, 3)

            all_prices = [r["price_numeric"] for r in listing_history
                          if r["price_numeric"] is not None]
            fig2 = Figure(figsize=(3.5, 3.5))
            self._dist_canvas = FigureCanvasQTAgg(fig2)
            self._dist_ax = fig2.add_subplot(111)
            self._setup_price_distribution(self._dist_ax, all_prices)
            fig2.tight_layout()
            charts_layout.addWidget(self._dist_canvas, 2)

            layout.addWidget(charts_widget)
        else:
            no_chart = QLabel("Install matplotlib for charts:  pip install matplotlib")
            no_chart.setStyleSheet("color: #888; padding: 12px 0;")
            layout.addWidget(no_chart)

        # --- Listings table ---
        table_label = QLabel("Most seen listings:")
        table_label.setStyleSheet("font-weight: bold; margin-top: 6px;")
        layout.addWidget(table_label)

        self._rows_data = self._build_rows_data(listing_history)
        self._listings_table = self._build_listings_table(self._rows_data)
        self._listings_table.itemDoubleClicked.connect(self._open_listing_url)
        self._listings_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._listings_table.customContextMenuRequested.connect(self._on_table_context_menu)
        layout.addWidget(self._listings_table, 1)

        # Wire interactive chart events now that the table is built
        if _MATPLOTLIB_OK:
            self._trend_canvas.mpl_connect("motion_notify_event", self._on_trend_hover)
            if self._hist_bins:
                self._dist_canvas.mpl_connect("button_press_event", self._on_dist_click)

        # --- Close button ---
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    # -- Price trend (hover tooltip) ---------------------------------------

    def _setup_price_trend(self, ax, runs) -> None:
        self._trend_runs = list(runs)
        x = list(range(len(runs)))
        labels = [r["run_id"][:16].replace("T", " ") for r in runs]
        avgs = [r["avg_price"] for r in runs]
        mins = [r["min_price"] for r in runs]
        maxs = [r["max_price"] for r in runs]
        counts = [r["listing_count"] for r in runs]

        ax_count = ax.twinx()
        ax_count.bar(x, counts, color="#cccccc", alpha=0.4, zorder=1)
        ax_count.set_ylabel("Listings", color="#aaa", fontsize=8)
        ax_count.tick_params(axis="y", labelcolor="#aaa", labelsize=7)
        ax_count.set_ylim(bottom=0)

        nan = float("nan")
        has_prices = any(v is not None for v in avgs)
        if has_prices:
            ax.plot(x, [v if v is not None else nan for v in avgs],
                    "b-o", label="Avg", linewidth=2, markersize=5, zorder=3)
            ax.plot(x, [v if v is not None else nan for v in mins],
                    color="#2ca02c", linestyle="--", marker="s",
                    label="Min", linewidth=1.2, markersize=4, zorder=3)
            ax.plot(x, [v if v is not None else nan for v in maxs],
                    color="#d62728", linestyle="--", marker="^",
                    label="Max", linewidth=1.2, markersize=4, zorder=3)
            ax.legend(loc="upper left", fontsize=8)

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=7)
        ax.set_ylabel("Price (€)", fontsize=9)
        ax.set_title("Price over time", fontsize=10)
        ax.grid(True, alpha=0.3, zorder=0)

        self._trend_annotation = ax.annotate(
            "",
            xy=(0, 0),
            xytext=(14, 14),
            textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#aaaaaa", alpha=0.92),
            arrowprops=dict(arrowstyle="->", color="#888888"),
            fontsize=8,
            zorder=10,
        )
        self._trend_annotation.set_visible(False)

    def _on_trend_hover(self, event) -> None:
        if self._trend_annotation is None:
            return
        if event.xdata is None:
            if self._trend_annotation.get_visible():
                self._trend_annotation.set_visible(False)
                self._trend_canvas.draw_idle()
            return

        xi = int(round(event.xdata))
        if not (0 <= xi < len(self._trend_runs)):
            if self._trend_annotation.get_visible():
                self._trend_annotation.set_visible(False)
                self._trend_canvas.draw_idle()
            return

        run = self._trend_runs[xi]

        def _p(v):
            return f"{v:.0f}€" if v is not None else "—"

        text = (
            f'{run["run_id"][:10]}\n'
            f'Avg: {_p(run["avg_price"])}\n'
            f'Min: {_p(run["min_price"])}   Max: {_p(run["max_price"])}\n'
            f'Listings: {run["listing_count"]}'
        )
        y_anchor = next(
            (v for v in (run["avg_price"], run["min_price"], run["max_price"])
             if v is not None),
            0,
        )
        self._trend_annotation.set_text(text)
        self._trend_annotation.xy = (xi, y_anchor)
        self._trend_annotation.set_visible(True)
        self._trend_canvas.draw_idle()

    # -- Price distribution (click-to-filter) ------------------------------

    def _setup_price_distribution(self, ax, prices: list[float]) -> None:
        if not prices:
            ax.text(0.5, 0.5, "No numeric\nprices", ha="center", va="center",
                    transform=ax.transAxes, fontsize=10, color="#888")
            ax.set_title("Price distribution", fontsize=10)
            return

        bins = min(30, max(5, len(prices) // 5))
        _, bin_edges, patches = ax.hist(
            prices, bins=bins, color="steelblue", edgecolor="white", alpha=0.85
        )
        avg = sum(prices) / len(prices)
        ax.axvline(avg, color="orange", linewidth=1.5, linestyle="--",
                   label=f"Avg {avg:.0f}€")
        ax.legend(fontsize=8)
        ax.set_xlabel("Price (€)", fontsize=9)
        ax.set_ylabel("Listings", fontsize=9)
        ax.set_title("Price distribution — click bar to filter", fontsize=10)
        ax.grid(True, alpha=0.3, axis="y")

        self._hist_patches = list(patches)
        self._hist_bins = list(bin_edges)

    def _on_dist_click(self, event) -> None:
        if event.xdata is None or not self._hist_bins:
            return

        clicked = None
        for i, (lo, hi) in enumerate(zip(self._hist_bins[:-1], self._hist_bins[1:])):
            if lo <= event.xdata <= hi:
                clicked = i
                break
        if clicked is None:
            return

        if clicked == self._active_bin:
            self._active_bin = None
            for patch in self._hist_patches:
                patch.set_facecolor("steelblue")
                patch.set_alpha(0.85)
            self._show_all_table_rows()
        else:
            self._active_bin = clicked
            lo = self._hist_bins[clicked]
            hi = self._hist_bins[clicked + 1]
            for i, patch in enumerate(self._hist_patches):
                patch.set_facecolor("orange" if i == clicked else "steelblue")
                patch.set_alpha(1.0 if i == clicked else 0.3)
            self._filter_table_by_price(lo, hi)

        self._dist_canvas.draw_idle()

    def _filter_table_by_price(self, lo: float, hi: float) -> None:
        if self._listings_table is None:
            return
        for row in range(self._listings_table.rowCount()):
            avg_item = self._listings_table.item(row, 4)  # Avg € column
            avg = avg_item.data(Qt.ItemDataRole.UserRole) if avg_item else None
            self._listings_table.setRowHidden(row, not (avg is not None and lo <= avg <= hi))

    def _show_all_table_rows(self) -> None:
        if self._listings_table is None:
            return
        for row in range(self._listings_table.rowCount()):
            self._listings_table.setRowHidden(row, False)

    # -- Table context menu ------------------------------------------------

    def _row_data_at(self, visual_row: int) -> dict | None:
        if self._listings_table is None:
            return None
        title_item = self._listings_table.item(visual_row, 0)
        if title_item is None:
            return None
        lid = title_item.data(Qt.ItemDataRole.UserRole + 1)
        return next((r for r in self._rows_data if r["listing_id"] == lid), None)

    def _on_table_context_menu(self, pos) -> None:
        if self._listings_table is None:
            return
        item = self._listings_table.itemAt(pos)
        if item is None:
            return
        row_data = self._row_data_at(item.row())
        if row_data is None:
            return

        menu = QMenu(self)
        open_action   = menu.addAction("Open listing")
        menu.addSeparator()
        save_action   = menu.addAction("Save to database")
        ignore_action = menu.addAction("Ignore listing")

        action = menu.exec(self._listings_table.viewport().mapToGlobal(pos))
        if action == open_action:
            _open_url_safe(row_data["url"])
        elif action == save_action:
            self._save_listing(row_data)
        elif action == ignore_action:
            self._ignore_listing(item.row(), row_data)

    def _make_listing(self, row_data: dict) -> Listing:
        return Listing(
            id=row_data["listing_id"],
            title=row_data["title"],
            price=row_data["price_raw"],
            city="",
            district="",
            date="",
            url=row_data["url"],
            image_url=None,
        )

    def _save_listing(self, row_data: dict) -> None:
        result = db.save_listings(self.conn, [self._make_listing(row_data)])
        if result.added:
            QMessageBox.information(self, "Saved",
                f'"{row_data["title"]}" added to the database.')
        elif result.updated:
            QMessageBox.information(self, "Updated",
                f'"{row_data["title"]}" was already saved — updated.')
        else:
            QMessageBox.information(self, "Already saved",
                f'"{row_data["title"]}" is already in the database unchanged.')

    def _ignore_listing(self, visual_row: int, row_data: dict) -> None:
        db.ignore_listing(self.conn, self._make_listing(row_data))
        if self._listings_table is not None:
            self._listings_table.setRowHidden(visual_row, True)
        self._rows_data = [r for r in self._rows_data if r["listing_id"] != row_data["listing_id"]]

    # -- Listings table ----------------------------------------------------

    @staticmethod
    def _open_listing_url(item: QTableWidgetItem) -> None:
        url_item = item.tableWidget().item(item.row(), 0)
        if url_item:
            _open_url_safe(url_item.data(Qt.ItemDataRole.UserRole) or "")

    @staticmethod
    def _build_rows_data(listing_history) -> list[dict]:
        by_id: dict[str, list] = {}
        for row in listing_history:
            lid = row["listing_id"]
            if lid not in by_id:
                by_id[lid] = []
            by_id[lid].append(row)

        rows_data = []
        for lid, rows in by_id.items():
            prices = [r["price_numeric"] for r in rows if r["price_numeric"] is not None]
            price_changes = sum(
                1 for a, b in zip(rows, rows[1:])
                if a["price_numeric"] != b["price_numeric"]
                and a["price_numeric"] is not None
                and b["price_numeric"] is not None
            )
            rows_data.append({
                "listing_id": lid,
                "title": next((r["title"] for r in rows if r["title"]), lid),
                "url": next((r["url"] for r in reversed(rows) if r["url"]), ""),
                "price_raw": rows[-1]["price_raw"] or "",
                "sessions": len(rows),
                "min_price": min(prices) if prices else None,
                "max_price": max(prices) if prices else None,
                "avg_price": sum(prices) / len(prices) if prices else None,
                "changes": price_changes,
                "last_run": max(r["run_id"] for r in rows)[:10],
            })

        rows_data.sort(key=lambda r: r["sessions"], reverse=True)
        return rows_data[:50]

    @staticmethod
    def _build_listings_table(rows_data: list[dict]) -> QTableWidget:
        columns = ["Title", "Sessions", "Min €", "Max €", "Avg €", "Price changes", "Last seen"]
        table = QTableWidget(len(rows_data), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setToolTip("Double-click to open · Right-click for more options")
        table.setSortingEnabled(False)

        def _fmt(v: float | None) -> str:
            return f"{v:.0f}" if v is not None else "—"

        def _n(val: float | None, text: str) -> _SortableItem:
            item = _SortableItem(text)
            item.setData(Qt.ItemDataRole.UserRole, val)
            return item

        for i, s in enumerate(rows_data):
            title_item = QTableWidgetItem(s["title"])
            title_item.setData(Qt.ItemDataRole.UserRole, s["url"])
            title_item.setData(Qt.ItemDataRole.UserRole + 1, s["listing_id"])
            table.setItem(i, 0, title_item)
            table.setItem(i, 1, _n(s["sessions"],  str(s["sessions"])))
            table.setItem(i, 2, _n(s["min_price"], _fmt(s["min_price"])))
            table.setItem(i, 3, _n(s["max_price"], _fmt(s["max_price"])))
            table.setItem(i, 4, _n(s["avg_price"], _fmt(s["avg_price"])))
            table.setItem(i, 5, _n(s["changes"],   str(s["changes"])))
            table.setItem(i, 6, QTableWidgetItem(s["last_run"]))

        table.setSortingEnabled(True)
        table.sortItems(1, Qt.SortOrder.DescendingOrder)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        return table


class SavedSearchRow(QFrame):
    """One row in the Home tab's saved-searches list: a description plus
    "Run" (restore these filters on the Search tab and search again) and
    "Delete" (forget this saved search) buttons — mirrors the way `ListingCard`
    is embedded in a `QListWidget` via `setItemWidget`.
    """

    run_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, row, description: str):
        super().__init__()
        self.row = row
        self.setToolTip("Double-click to run this search")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)

        label = QLabel(description)
        label.setWordWrap(True)
        layout.addWidget(label, 1)

        run_button = QPushButton("Run")
        run_button.setToolTip("Search again using this saved query and filters")
        run_button.clicked.connect(lambda: self.run_requested.emit(self.row))
        layout.addWidget(run_button)

        delete_button = QPushButton("Delete")
        delete_button.setToolTip("Remove this saved search")
        delete_button.clicked.connect(lambda: self.delete_requested.emit(self.row))
        layout.addWidget(delete_button)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.run_requested.emit(self.row)
        super().mouseDoubleClickEvent(event)


THUMBNAIL_WIDTH = 64
THUMBNAIL_HEIGHT = 48

# Custom item-data roles (stored on each row's column-0 item, regardless of how
# the user has reordered/hidden columns) so selection and double-click lookups
# always find the right listing id / URL.
ID_ROLE = Qt.ItemDataRole.UserRole
URL_ROLE = Qt.ItemDataRole.UserRole + 1

# A `(label, field_name)` pair with `field_name=None` marks the thumbnail
# column — it's populated specially (an async-loaded icon) rather than as text.
IMAGE_COLUMN = ("Image", None)

SAVED_COLUMNS = [
    IMAGE_COLUMN,
    ("Title", "title"),
    ("Price", "price"),
    ("District", "district"),
    ("Date posted", "date"),
    ("First seen", "first_seen"),
    ("Last seen", "last_seen"),
]

HIDDEN_COLUMNS = [
    IMAGE_COLUMN,
    ("Title", "title"),
    ("Price", "price"),
    ("District", "district"),
    ("Date posted", "date"),
    ("Hidden on", "ignored_at"),
]


class DatabaseView(QWidget):
    """Browsable table of saved listings and ignored ads, sharing one `QTableWidget`.

    A toggle button switches the same table — same styling, selection,
    thumbnails and column controls — between saved listings (most recently
    seen first) and hidden/ignored ads (most recently hidden first), renaming
    its tab (`mode_changed`) and the row-action button to match. Double-clicking
    a row opens its URL in the browser, mirroring `ListingCard`'s click-to-open
    behaviour. Right-clicking the header lets you show/hide columns; dragging
    headers reorders them and dragging their edges resizes them (native `QHeaderView`
    behaviour — `setSectionsMovable` is the only thing that needed enabling).
    """

    mode_changed = Signal(bool)

    def __init__(self, connection, network_manager: QNetworkAccessManager):
        super().__init__()
        self.connection = connection
        self.network_manager = network_manager
        self.thumbnail_cache: dict[str, QPixmap] = {}
        self.showing_hidden = False
        self._sized_modes: set[bool] = set()

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.count_label = QLabel("No saved listings yet")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch()
        self.toggle_button = QPushButton("Show hidden ads")
        self.toggle_button.clicked.connect(self._toggle_mode)
        toolbar.addWidget(self.toggle_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMBNAIL_HEIGHT + 8)
        self.table.setIconSize(QSize(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT))
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.itemSelectionChanged.connect(self._update_selection_label)

        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_column_menu)
        layout.addWidget(self.table)

        bottom = QHBoxLayout()
        self.hint_label = QLabel("Double-click a row to open the ad in your browser.")
        self.hint_label.setStyleSheet("color: #888; font-size: 12px;")
        bottom.addWidget(self.hint_label)
        bottom.addStretch()
        self.selection_label = QLabel("")
        self.selection_label.setStyleSheet("color: #555; font-size: 12px; padding-right: 8px;")
        bottom.addWidget(self.selection_label)
        self.action_button = QPushButton("Remove selected")
        self.action_button.clicked.connect(self._on_action_clicked)
        bottom.addWidget(self.action_button)
        layout.addLayout(bottom)

        self._apply_mode()

    # -- Mode toggle --------------------------------------------------------

    def _toggle_mode(self) -> None:
        self.showing_hidden = not self.showing_hidden
        self._apply_mode()
        self.refresh()
        self.mode_changed.emit(self.showing_hidden)

    def _apply_mode(self) -> None:
        columns = HIDDEN_COLUMNS if self.showing_hidden else SAVED_COLUMNS
        self.toggle_button.setText("Show saved listings" if self.showing_hidden else "Show hidden ads")
        self.action_button.setText("Unhide selected" if self.showing_hidden else "Remove selected")
        self.hint_label.setText(
            "Double-click a hidden ad to open it in your browser — sorted by most recently hidden."
            if self.showing_hidden else
            "Double-click a row to open the ad in your browser."
        )

        self.table.setSortingEnabled(False)
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels([label for label, _ in columns])
        self.table.setSortingEnabled(True)
        # Column layout was just rebuilt for this mode — give it sensible
        # content-based widths once, then leave the user's resizing alone.
        self._sized_modes.discard(self.showing_hidden)
        self._update_selection_label()

    # -- Loading -------------------------------------------------------------

    def refresh(self) -> None:
        columns = HIDDEN_COLUMNS if self.showing_hidden else SAVED_COLUMNS
        rows = db.fetch_ignored(self.connection) if self.showing_hidden else db.fetch_all(self.connection)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, (_, field_name) in enumerate(columns):
                if field_name is None:
                    item = QTableWidgetItem()
                    item.setData(ID_ROLE, row["id"])
                    item.setData(URL_ROLE, row["url"])
                    self.table.setItem(row_index, column_index, item)
                    self._set_thumbnail(row["id"], row["image_url"])
                else:
                    self.table.setItem(row_index, column_index, QTableWidgetItem(row[field_name] or ""))
        self.table.setSortingEnabled(True)

        if self.showing_hidden not in self._sized_modes:
            self.table.resizeColumnsToContents()
            self._sized_modes.add(self.showing_hidden)

        if self.showing_hidden:
            self.count_label.setText(f"{len(rows)} hidden ad(s)" if rows else "No hidden ads yet")
        else:
            self.count_label.setText(
                f"{len(rows)} listing(s) in the database" if rows else "No saved listings yet"
            )
        self._update_selection_label()

    # -- Thumbnails -----------------------------------------------------------

    def _set_thumbnail(self, listing_id: str, image_url: str | None) -> None:
        if not image_url:
            return

        cached = self.thumbnail_cache.get(listing_id)
        if cached is not None:
            self._apply_thumbnail(listing_id, cached)
            return

        reply = self.network_manager.get(QNetworkRequest(QUrl(image_url)))
        reply.finished.connect(lambda: self._on_thumbnail_loaded(reply, listing_id))

    def _on_thumbnail_loaded(self, reply: QNetworkReply, listing_id: str) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return

        pixmap = QPixmap()
        if not pixmap.loadFromData(reply.readAll()):
            return

        cropped = cover_pixmap(pixmap, THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        self.thumbnail_cache[listing_id] = cropped
        self._apply_thumbnail(listing_id, cropped)

    def _apply_thumbnail(self, listing_id: str, pixmap: QPixmap) -> None:
        # Looked up by id (rather than a remembered row index) so a thumbnail
        # that finishes loading after a sort/refresh still lands on the right row.
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(ID_ROLE) == listing_id:
                item.setIcon(QIcon(pixmap))
                return

    # -- Selection & row actions ----------------------------------------------

    def _selected_ids(self) -> list[str]:
        ids = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), 0)
            listing_id = item.data(ID_ROLE) if item is not None else None
            if listing_id:
                ids.append(listing_id)
        return ids

    def _update_selection_label(self) -> None:
        count = len(self.table.selectionModel().selectedRows())
        self.selection_label.setText(f"{count} selected" if count else "")

    def _on_action_clicked(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Nothing selected", "Select one or more rows first.")
            return

        if self.showing_hidden:
            question = f'Unhide {len(ids)} ad(s)? They\'ll be eligible to reappear in future search results.'
        else:
            question = f"Remove {len(ids)} listing(s) from the database? This can't be undone."

        choice = QMessageBox.question(
            self, "Confirm", question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        if self.showing_hidden:
            db.unignore_listings(self.connection, ids)
        else:
            db.delete_listings(self.connection, ids)
        self.refresh()

    # -- Column visibility -----------------------------------------------------

    def _show_column_menu(self, pos) -> None:
        header = self.table.horizontalHeader()
        menu = QMenu(self)
        for index in range(self.table.columnCount()):
            label = self.table.horizontalHeaderItem(index).text()
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(index))
            action.toggled.connect(lambda checked, column=index: self.table.setColumnHidden(column, not checked))
        menu.exec(header.mapToGlobal(pos))

    # -- Opening ---------------------------------------------------------------

    def _open_row(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is None:
            return
        url = item.data(URL_ROLE)
        if url:
            _open_url_safe(url)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OLX Listings Browser")
        self.resize(1100, 750)

        self.network_manager = QNetworkAccessManager(self)
        self.listings: list[Listing] = []
        self.cards: list[ListingCard] = []
        self.pixmap_cache: dict[str, QPixmap] = {}
        self.thread: QThread | None = None
        self.worker: ScrapeWorker | None = None
        self.category_thread: QThread | None = None
        self.category_loader: CategoryLoader | None = None
        self.region_thread: QThread | None = None
        self.region_loader: RegionLoader | None = None
        self.city_loaders: list[tuple[QThread, CityLoader]] = []
        self._categories_ready = False
        self._regions_ready = False
        self._filter_error: str | None = None
        self._loaded_categories: dict[str, dict] = {}
        self._loaded_regions: list[dict] = []
        self._running_item_id: int | None = None
        self._hidden_listings: list[Listing] = []
        self._hidden_items: list[QListWidgetItem] = []
        self._show_hidden: bool = False
        self._last_clicked_card: ListingCard | None = None
        self._last_search_url: str | None = None
        self.db_connection = db.connect(DB_PATH)

        self.search_page = QWidget()
        search_layout = QVBoxLayout(self.search_page)
        search_layout.addLayout(self._build_search_bar())
        search_layout.addLayout(self._build_results_toolbar())

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setWrapping(True)
        self.list_widget.setSpacing(8)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.list_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.list_widget.setStyleSheet("background: #f4f4f4;")
        search_layout.addWidget(self.list_widget)

        self.selection_label = QLabel("")
        self.selection_label.setStyleSheet("color: #555; font-size: 12px; padding: 2px 4px;")
        self.selection_label.setVisible(False)
        search_layout.addWidget(self.selection_label)

        self.database_view = DatabaseView(self.db_connection, self.network_manager)
        self.database_view.mode_changed.connect(self._on_database_mode_changed)

        self.tabs = QTabWidget()
        self.home_page = self._build_home_page()
        self.tabs.addTab(self.home_page, "Home")
        self.tabs.addTab(self.search_page, "Search")
        self.tabs.addTab(self.database_view, "Database")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())
        self.filter_status_label = QLabel("⏳ Loading filters…")
        self.filter_status_label.setToolTip(
            "Whether OLX's category and district lists have finished loading — "
            'needed so that "Run" on a saved search can restore its filters correctly'
        )
        self.statusBar().addPermanentWidget(self.filter_status_label)
        self._start_category_loading()
        self._start_region_loading()
        self.database_view.refresh()

    def _build_home_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("OLX Listings Browser")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        # Items section
        items_header = QHBoxLayout()
        items_label = QLabel("Items")
        items_label.setStyleSheet("font-weight: bold;")
        items_header.addWidget(items_label)
        items_header.addStretch()
        new_item_btn = QPushButton("New Item")
        new_item_btn.setToolTip("Create a named item to group multiple searches together")
        new_item_btn.clicked.connect(self._create_new_item)
        items_header.addWidget(new_item_btn)
        layout.addLayout(items_header)

        self.items_list = QListWidget()
        self.items_list.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.items_list, 2)

        # Saved searches section
        saved_label = QLabel("Saved searches")
        saved_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(saved_label)

        self.saved_searches_list = QListWidget()
        self.saved_searches_list.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.saved_searches_list, 1)

        self._refresh_items()
        self._refresh_saved_searches()
        return page

    # -- Items -------------------------------------------------------------

    def _refresh_items(self) -> None:
        rows = db.fetch_items(self.db_connection)
        self.items_list.clear()
        if not rows:
            placeholder = QListWidgetItem(
                'No items yet — click "New Item" to create one, then use "Add to Item" on the Search tab.'
            )
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.items_list.addItem(placeholder)
            return
        for row in rows:
            widget = ItemRow(row)
            widget.run_requested.connect(self._run_item)
            widget.analytics_requested.connect(self._show_item_analytics)
            widget.edit_requested.connect(self._edit_item)
            widget.delete_requested.connect(self._delete_item)
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setSizeHint(widget.sizeHint())
            self.items_list.addItem(item)
            self.items_list.setItemWidget(item, widget)

    @staticmethod
    def _describe_item_searches(searches) -> str:
        if not searches:
            return ""
        parts = []
        for s in searches:
            desc = f'"{s["query"]}"'
            if s["category_label"]:
                desc += f" / {s['category_label']}"
            loc = " / ".join(l for l in (s["region_label"], s["city_label"]) if l)
            if loc:
                desc += f" / {loc}"
            parts.append(desc)
        return " + ".join(parts)

    def _create_new_item(self) -> None:
        name, ok = QInputDialog.getText(self, "New Item", "Item name:")
        if not ok or not name.strip():
            return
        db.create_item(self.db_connection, name.strip())
        self._refresh_items()
        self.statusBar().showMessage(
            f'Item "{name.strip()}" created — go to the Search tab and use "Add to Item" to add searches.',
            6000,
        )

    def _run_item(self, row) -> None:
        if self.thread is not None:
            self.statusBar().showMessage(
                "A search is already running — wait for it to finish before running an item.", 5000
            )
            return
        searches = db.fetch_item_searches(self.db_connection, row["id"])
        if not searches:
            QMessageBox.information(
                self,
                "No searches",
                f'Item "{row["name"]}" has no searches yet.\n\n'
                'Go to the Search tab, configure a search, and click "Add to Item".',
            )
            return

        search_kwargs = [
            dict(
                query=s["query"],
                max_pages=s["pages"],
                delay=1.0,
                category_path=s["category_path"] or None,
                region_id=s["region_id"],
                city_id=s["city_id"],
                sort=s["sort"] or None,
            )
            for s in searches
        ]

        self._running_item_id = row["id"]
        self._last_search_url = None
        self.open_in_browser_button.setEnabled(False)
        self.tabs.setCurrentWidget(self.search_page)
        self.count_label.setText(f'Running item "{row["name"]}"…')
        self.search_button.setEnabled(False)
        self.search_button.setText("⏳")
        self.statusBar().showMessage(
            f'Running {len(searches)} search(es) for item "{row["name"]}"…'
        )

        self.thread = QThread(self)
        self.worker = MultiScrapeWorker(search_kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_item_search_progress)
        self.worker.finished.connect(self._on_search_finished)
        self.worker.failed.connect(self._on_search_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.start()

    def _on_item_search_progress(self, current: int, total: int, query: str) -> None:
        self.statusBar().showMessage(f"Search {current}/{total}: {query!r}…")

    def _edit_item(self, row) -> None:
        searches = db.fetch_item_searches(self.db_connection, row["id"])
        dialog = ItemEditDialog(
            row, searches, self.db_connection,
            categories=self._loaded_categories,
            regions=self._loaded_regions,
            parent=self,
        )
        dialog.exec()
        self._refresh_items()
        if dialog._pending_run_search is not None:
            self._run_single_search(dialog._pending_run_search)

    def _delete_item(self, row) -> None:
        choice = QMessageBox.question(
            self,
            "Delete item",
            f'Delete item "{row["name"]}" and all its searches?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        db.delete_item(self.db_connection, row["id"])
        self._refresh_items()

    def _show_item_analytics(self, row) -> None:
        dialog = ItemAnalyticsDialog(row["name"], row["id"], self.db_connection, parent=self)
        dialog.exec()

    def _add_to_item(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            QMessageBox.warning(self, "Missing search term", "Type something to search for first.")
            return

        items = db.fetch_items(self.db_connection)

        dialog = QDialog(self)
        dialog.setWindowTitle("Add to Item")
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.addWidget(QLabel("Add the current search to which item?"))

        combo = QComboBox()
        combo.addItem("— New item… —", None)
        for it in items:
            n = len(db.fetch_item_searches(self.db_connection, it["id"]))
            combo.addItem(f'{it["name"]}  ({n} search{"es" if n != 1 else ""})', it["id"])
        dialog_layout.addWidget(combo)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("Add")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        dialog_layout.addLayout(btn_row)

        if not dialog.exec():
            return

        item_id = combo.currentData()
        item_name = combo.currentText()

        if item_id is None:
            name, ok = QInputDialog.getText(dialog, "New Item", "Item name:")
            if not ok or not name.strip():
                return
            item_id = db.create_item(self.db_connection, name.strip())
            item_name = name.strip()

        category = self.category_picker.selected_category()
        region = self.location_picker.selected_region()
        city = self.location_picker.selected_city()

        db.add_item_search(
            self.db_connection,
            item_id,
            query=query,
            pages=self.pages_spin.value(),
            category_path=category["path"] if category else None,
            category_label=category["name"] if category else None,
            region_id=region["id"] if region else None,
            region_label=region["label"] if region else None,
            city_id=city["id"] if city else None,
            city_label=city["label"] if city else None,
            sort=self.order_combo.currentData(),
        )
        self._refresh_items()
        self.statusBar().showMessage(
            f'Added "{query}" to item "{item_name}" — find it on the Home tab.', 6000
        )

    def _run_item_from_search(self) -> None:
        """Show an item picker and run the chosen item's searches on the Search tab."""
        if self.thread is not None:
            self.statusBar().showMessage(
                "A search is already running — wait for it to finish before running an item.", 5000
            )
            return

        items = db.fetch_items(self.db_connection)
        if not items:
            QMessageBox.information(
                self, "No items",
                'No items yet — create one on the Home tab first, then add searches to it.'
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Run Item")
        dlayout = QVBoxLayout(dialog)
        dlayout.addWidget(QLabel("Which item do you want to run?"))
        combo = QComboBox()
        for it in items:
            n = len(db.fetch_item_searches(self.db_connection, it["id"]))
            combo.addItem(f'{it["name"]}  ({n} search{"es" if n != 1 else ""})', it["id"])
        dlayout.addWidget(combo)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("Run")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        dlayout.addLayout(btn_row)

        if not dialog.exec():
            return

        item_id = combo.currentData()
        for it in items:
            if it["id"] == item_id:
                self._run_item(it)
                break

    # -- Saved searches ----------------------------------------------------

    def _refresh_saved_searches(self) -> None:
        rows = db.fetch_saved_searches(self.db_connection)
        self.saved_searches_list.clear()
        if not rows:
            placeholder = QListWidgetItem('No saved searches yet — use "Save search" on the Search tab to remember one.')
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.saved_searches_list.addItem(placeholder)
            return
        for row in rows:
            widget = SavedSearchRow(row, self._describe_saved_search(row))
            widget.run_requested.connect(self._run_saved_search)
            widget.delete_requested.connect(self._delete_saved_search)
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setSizeHint(widget.sizeHint())
            self.saved_searches_list.addItem(item)
            self.saved_searches_list.setItemWidget(item, widget)

    def _run_saved_search(self, row) -> None:
        """Restore a saved search's query and filters on the Search tab and run it."""
        if self.thread is not None:
            self.statusBar().showMessage(
                "A search is already running — wait for it to finish before running a saved search.", 5000
            )
            return

        self.tabs.setCurrentWidget(self.search_page)

        self.query_edit.setText(row["query"])
        self.pages_spin.setValue(row["pages"])
        self.category_picker.select_by_path(row["category_path"])
        self.location_picker.select_by_ids(row["region_id"], row["city_id"])
        # self.min_price_spin.setValue(row["min_price"] or 0)  # commented out — not needed for now
        # self.max_price_spin.setValue(row["max_price"] or 0)

        sort_index = self.order_combo.findData(row["sort"])
        self.order_combo.setCurrentIndex(sort_index if sort_index != -1 else 0)

        self._start_search()

    def _delete_saved_search(self, row) -> None:
        choice = QMessageBox.question(
            self, "Delete saved search",
            f'Delete the saved search "{row["query"]}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        db.delete_saved_search(self.db_connection, row["id"])
        self._refresh_saved_searches()

    @staticmethod
    def _describe_saved_search(row) -> str:
        parts = [f'"{row["query"]}"']
        if row["category_label"]:
            parts.append(row["category_label"])
        location = " / ".join(label for label in (row["region_label"], row["city_label"]) if label)
        if location:
            parts.append(location)
        if row["min_price"] is not None or row["max_price"] is not None:
            low = "" if row["min_price"] is None else f'{row["min_price"]:.0f}'
            high = "" if row["max_price"] is None else f'{row["max_price"]:.0f}'
            parts.append(f"{low}–{high} €")
        if row["sort"]:
            parts.append(row["sort"])
        parts.append(f"{row['pages']} page(s)")
        parts.append(f"saved {row['created_at'][:10]}")
        return " · ".join(parts)

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.database_view:
            self.database_view.refresh()
        elif widget is self.home_page:
            self._refresh_items()
            self._refresh_saved_searches()

    def _on_database_mode_changed(self, showing_hidden: bool) -> None:
        index = self.tabs.indexOf(self.database_view)
        self.tabs.setTabText(index, "Hidden ads" if showing_hidden else "Database")

    # -- Filter taxonomies (categories, districts, cities) -----------------
    #
    # Three independent things load asynchronously here — the category tree,
    # the district list, and (cascading from whichever district is picked) the
    # city list — because each is a real HTTP round trip the GUI shouldn't
    # block on. `filter_status_label` collapses the first two into a single
    # bottom-right readiness indicator (the "could I have a status ready
    # checkmark" request): "Run" on a saved search can only restore a category
    # or district filter correctly once its picker has been populated, so the
    # user knows to wait for "✓" before relying on that. Cities don't need
    # their own readiness flag — `LocationPicker` already fetches them on
    # demand and `select_by_ids` queues a pending city selection until that
    # district's batch arrives.

    def _start_category_loading(self) -> None:
        self.category_thread = QThread(self)
        self.category_loader = CategoryLoader()
        self.category_loader.moveToThread(self.category_thread)
        self.category_thread.started.connect(self.category_loader.run)
        self.category_loader.loaded.connect(self._on_categories_loaded)
        self.category_loader.failed.connect(self._on_categories_failed)
        self.category_loader.loaded.connect(self.category_thread.quit)
        self.category_loader.failed.connect(self.category_thread.quit)
        self.category_thread.finished.connect(self._on_category_thread_finished)
        self.category_thread.start()

    def _on_category_thread_finished(self) -> None:
        self.category_thread.deleteLater()
        self.category_loader.deleteLater()
        self.category_thread = None
        self.category_loader = None

    def _on_categories_loaded(self, categories: dict[str, dict]) -> None:
        self._loaded_categories = categories
        self.category_picker.set_categories(categories)
        self._categories_ready = True
        self._update_filter_status()

    def _on_categories_failed(self, message: str) -> None:
        self.category_picker.show_error("Categories unavailable")
        self._update_filter_status(error=f"Could not load OLX categories: {message}")

    def _start_region_loading(self) -> None:
        self.region_thread = QThread(self)
        self.region_loader = RegionLoader()
        self.region_loader.moveToThread(self.region_thread)
        self.region_thread.started.connect(self.region_loader.run)
        self.region_loader.loaded.connect(self._on_regions_loaded)
        self.region_loader.failed.connect(self._on_regions_failed)
        self.region_loader.loaded.connect(self.region_thread.quit)
        self.region_loader.failed.connect(self.region_thread.quit)
        self.region_thread.finished.connect(self._on_region_thread_finished)
        self.region_thread.start()

    def _on_region_thread_finished(self) -> None:
        self.region_thread.deleteLater()
        self.region_loader.deleteLater()
        self.region_thread = None
        self.region_loader = None

    def _on_regions_loaded(self, regions: list[dict]) -> None:
        self._loaded_regions = regions
        self.location_picker.set_regions(regions)
        self._regions_ready = True
        self._update_filter_status()

    def _on_regions_failed(self, message: str) -> None:
        self.location_picker.show_error("Districts unavailable")
        self._update_filter_status(error=f"Could not load OLX districts: {message}")

    def _update_filter_status(self, error: str | None = None) -> None:
        if error:
            self._filter_error = error
        if self._filter_error:
            self.filter_status_label.setText("✗ Filters unavailable")
            self.filter_status_label.setToolTip(self._filter_error)
            self.statusBar().showMessage(self._filter_error, 8000)
        elif self._categories_ready and self._regions_ready:
            self.filter_status_label.setText("✓ Filters ready")
            self.filter_status_label.setToolTip(
                'Category and district filters are loaded — "Run" on a saved '
                "search will restore them correctly"
            )
        else:
            self.filter_status_label.setText("⏳ Loading filters…")

    def _start_city_loading(self, region_id: int) -> None:
        thread = QThread(self)
        loader = CityLoader(region_id)
        loader.moveToThread(thread)
        thread.started.connect(loader.run)
        loader.loaded.connect(self._on_cities_loaded)
        loader.failed.connect(self._on_cities_failed)
        loader.loaded.connect(thread.quit)
        loader.failed.connect(thread.quit)
        thread.finished.connect(lambda: self._on_city_thread_finished(thread, loader))
        self.city_loaders.append((thread, loader))
        thread.start()

    def _on_city_thread_finished(self, thread: QThread, loader: CityLoader) -> None:
        thread.deleteLater()
        loader.deleteLater()
        if (thread, loader) in self.city_loaders:
            self.city_loaders.remove((thread, loader))

    def _on_cities_loaded(self, region_id: int, cities: list[dict]) -> None:
        self.location_picker.set_cities(region_id, cities)

    def _on_cities_failed(self, region_id: int, message: str) -> None:
        self.location_picker.city_lookup_failed(region_id)
        self.statusBar().showMessage(f"Could not load OLX cities for that district: {message}", 8000)

    # -- UI construction -------------------------------------------------

    def _build_search_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Search term, e.g. iphone 13")
        self.query_edit.returnPressed.connect(self._start_search)
        bar.addWidget(self.query_edit, 2)

        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(1, 50)
        self.pages_spin.setValue(1)
        self.pages_spin.setPrefix("Pages: ")
        bar.addWidget(self.pages_spin)

        self.category_picker = CategoryPicker()
        bar.addWidget(self.category_picker, 1)

        self.location_picker = LocationPicker()
        self.location_picker.city_lookup_requested.connect(self._start_city_loading)
        bar.addWidget(self.location_picker, 1)

        # Price range filter — commented out, not needed for now. The fields
        # remain in `db.save_search`/`search()` (and are still shown in
        # `_describe_saved_search` for searches saved before this change), so
        # re-enabling this is just a matter of uncommenting the widgets below
        # plus their `_on_search_criteria_changed` connections, `_start_search`
        # kwargs, `_save_search` params and `_run_saved_search` restoration.
        # self.min_price_spin = QDoubleSpinBox()
        # self.min_price_spin.setRange(0, 1_000_000)
        # self.min_price_spin.setDecimals(0)
        # self.min_price_spin.setSpecialValueText("Min €")
        # self.min_price_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        # bar.addWidget(self.min_price_spin)
        #
        # self.max_price_spin = QDoubleSpinBox()
        # self.max_price_spin.setRange(0, 1_000_000)
        # self.max_price_spin.setDecimals(0)
        # self.max_price_spin.setSpecialValueText("Max €")
        # self.max_price_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        # bar.addWidget(self.max_price_spin)

        self.order_combo = QComboBox()
        self.order_combo.addItem("Default Search", None)
        for key in sorted(SORT_OPTIONS):
            self.order_combo.addItem(key, key)
        self.order_combo.setCurrentIndex(self.order_combo.findData("newest"))
        bar.addWidget(self.order_combo)

        self.search_button = QPushButton("🔍")
        self.search_button.setToolTip("Search")
        self.search_button.setFixedWidth(36)
        self.search_button.clicked.connect(self._start_search)
        bar.addWidget(self.search_button)

        return bar

    def _build_results_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()

        self.count_label = QLabel("No listings yet")
        toolbar.addWidget(self.count_label)
        toolbar.addStretch()

        self.save_search_button = QPushButton("Save search")
        self.save_search_button.setToolTip("Remember the current query and filters — find them again on the Home tab")
        self.save_search_button.clicked.connect(self._save_search)
        toolbar.addWidget(self.save_search_button)

        self.add_to_item_button = QPushButton("Add to Item")
        self.add_to_item_button.setToolTip(
            "Add the current search to an item — items combine multiple searches into one result set"
        )
        self.add_to_item_button.clicked.connect(self._add_to_item)
        toolbar.addWidget(self.add_to_item_button)

        self.run_item_button = QPushButton("Run Item")
        self.run_item_button.setToolTip("Run all searches in an item and show combined results here")
        self.run_item_button.clicked.connect(self._run_item_from_search)
        toolbar.addWidget(self.run_item_button)

        self.toggle_hidden_button = QPushButton("Show hidden ads")
        self.toggle_hidden_button.setToolTip("Show/hide ads that you've already ignored from this search's results")
        self.toggle_hidden_button.setEnabled(False)
        self.toggle_hidden_button.clicked.connect(self._toggle_hidden_ads)
        toolbar.addWidget(self.toggle_hidden_button)

        self.open_in_browser_button = QPushButton("Open in browser")
        self.open_in_browser_button.setToolTip("Open this search on OLX.pt")
        self.open_in_browser_button.setEnabled(False)
        self.open_in_browser_button.clicked.connect(self._open_search_in_browser)
        toolbar.addWidget(self.open_in_browser_button)

        self.ignore_button = QPushButton("Ignore selected")
        self.ignore_button.setToolTip("Hide the selected listings from this and all future search results")
        self.ignore_button.clicked.connect(self._ignore_selected)
        toolbar.addWidget(self.ignore_button)

        self.save_button = QPushButton("Save selected to database")
        self.save_button.clicked.connect(self._save_selected)
        toolbar.addWidget(self.save_button)

        toolbar.addWidget(QLabel("Sort by:"))
        self.local_sort_combo = QComboBox()
        for label, value in LOCAL_SORT_OPTIONS:
            self.local_sort_combo.addItem(label, value)
        self.local_sort_combo.currentIndexChanged.connect(self._refresh_list)
        toolbar.addWidget(self.local_sort_combo)

        self.query_edit.textChanged.connect(self._on_search_criteria_changed)
        self.pages_spin.valueChanged.connect(self._on_search_criteria_changed)
        self.category_picker.selection_changed.connect(self._on_search_criteria_changed)
        self.location_picker.selection_changed.connect(self._on_search_criteria_changed)
        # self.min_price_spin.valueChanged.connect(self._on_search_criteria_changed)
        # self.max_price_spin.valueChanged.connect(self._on_search_criteria_changed)
        self.order_combo.currentIndexChanged.connect(self._on_search_criteria_changed)

        return toolbar

    def _on_search_criteria_changed(self) -> None:
        """Re-enable "Save search" once the query or any filter changes from what was last saved."""
        self.save_search_button.setEnabled(True)
        self.save_search_button.setToolTip("Remember the current query and filters — find them again on the Home tab")

    # -- Searching --------------------------------------------------------

    def _start_search(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            QMessageBox.warning(self, "Missing search term", "Type something to search for first.")
            return
        if self.thread is not None:
            return

        region = self.location_picker.selected_region()
        city = self.location_picker.selected_city()

        kwargs = dict(
            query=query,
            max_pages=self.pages_spin.value(),
            delay=1.0,
            category_path=self.category_picker.selected_path(),
            region_id=region["id"] if region else None,
            city_id=city["id"] if city else None,
            # min_price=self.min_price_spin.value() or None,  # commented out — not needed for now
            # max_price=self.max_price_spin.value() or None,
            min_price=None,
            max_price=None,
            sort=self.order_combo.currentData(),
        )

        base = (f"https://www.olx.pt/{kwargs['category_path']}/"
                if kwargs.get("category_path") else "https://www.olx.pt/ads/")
        url_params: dict = {"q": query}
        if kwargs.get("region_id"):
            url_params["search[region_id]"] = kwargs["region_id"]
        if kwargs.get("city_id"):
            url_params["search[city_id]"] = kwargs["city_id"]
        if kwargs.get("sort"):
            url_params["search[order]"] = SORT_OPTIONS.get(kwargs["sort"], kwargs["sort"])
        self._last_search_url = base + "?" + urlencode(url_params)

        self.search_button.setEnabled(False)
        self.search_button.setText("⏳")
        self.statusBar().showMessage(f"Scraping up to {kwargs['max_pages']} page(s) for {query!r}…")

        self.thread = QThread(self)
        self.worker = ScrapeWorker(kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._on_search_finished)
        self.worker.failed.connect(self._on_search_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.start()

    def _on_thread_finished(self) -> None:
        self.thread.deleteLater()
        self.worker.deleteLater()
        self.thread = None
        self.worker = None
        self._running_item_id = None
        self.search_button.setEnabled(True)
        self.search_button.setText("🔍")

    def _on_search_finished(self, listings: list[Listing]) -> None:
        if self._running_item_id is not None:
            db.save_price_snapshot_batch(self.db_connection, self._running_item_id, listings)
            self._running_item_id = None

        ignored_ids = db.fetch_ignored_ids(self.db_connection)
        self.listings = [l for l in listings if l.id not in ignored_ids]
        self._hidden_listings = [l for l in listings if l.id in ignored_ids]

        self._show_hidden = False
        self.toggle_hidden_button.setText("Show hidden ads")
        self.toggle_hidden_button.setEnabled(bool(self._hidden_listings))
        self.open_in_browser_button.setEnabled(bool(self._last_search_url))

        message = f"Found {len(self.listings)} listings"
        if self._hidden_listings:
            message += f" ({len(self._hidden_listings)} hidden — already ignored)"
        self.statusBar().showMessage(message, 6000)
        self._refresh_list()

    def _on_search_failed(self, message: str) -> None:
        self._running_item_id = None
        self.statusBar().clearMessage()
        QMessageBox.critical(self, "Search failed", message)

    def _save_search(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            QMessageBox.warning(self, "Missing search term", "Type something to search for first.")
            return

        category = self.category_picker.selected_category()
        region = self.location_picker.selected_region()
        city = self.location_picker.selected_city()
        db.save_search(
            self.db_connection,
            query=query,
            pages=self.pages_spin.value(),
            category_path=category["path"] if category else None,
            category_label=category["name"] if category else None,
            region_id=region["id"] if region else None,
            region_label=region["label"] if region else None,
            city_id=city["id"] if city else None,
            city_label=city["label"] if city else None,
            # min_price=self.min_price_spin.value() or None,  # commented out — not needed for now
            # max_price=self.max_price_spin.value() or None,
            min_price=None,
            max_price=None,
            sort=self.order_combo.currentData(),
        )
        self.statusBar().showMessage(f'Saved search "{query}" — find it on the Home tab', 6000)
        self.save_search_button.setEnabled(False)
        self.save_search_button.setToolTip("Already saved — change the query or filters to save again")
        self._refresh_saved_searches()

    # -- Database -----------------------------------------------------------

    def _save_selected(self) -> None:
        selected = [card.listing for card in self.cards if card.is_selected()]
        if not selected:
            QMessageBox.information(
                self, "Nothing selected",
                "Tick the checkbox in the corner of the listings you want to save first.",
            )
            return

        result = db.save_listings(self.db_connection, selected)

        summary = (
            f"{result.added} new, {result.updated} updated, "
            f"{result.unchanged} unchanged (last-seen refreshed) — saved to {DB_PATH}"
        )
        if result.changes:
            shown = result.changes[:15]
            summary += "\n\nChanges since last time:\n" + "\n".join(f"• {line}" for line in shown)
            if len(result.changes) > len(shown):
                summary += f"\n… and {len(result.changes) - len(shown)} more"

        QMessageBox.information(self, "Saved to database", summary)
        self.statusBar().showMessage(f"Saved {len(selected)} selected listings to {DB_PATH}", 6000)
        self.database_view.refresh()

    def closeEvent(self, event) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait()
        self.db_connection.close()
        super().closeEvent(event)

    # -- Results list ------------------------------------------------------

    def _refresh_list(self) -> None:
        # Hide existing item widgets before clearing — QListWidget.clear() removes
        # items but widgets set via setItemWidget() stay as viewport children.
        # Making them invisible first prevents them from showing as ghost cards.
        for i in range(self.list_widget.count()):
            w = self.list_widget.itemWidget(self.list_widget.item(i))
            if w:
                w.hide()

        ordered = self._sort_for_display(self.listings)

        self.list_widget.clear()
        self.cards = []
        self._hidden_items = []
        self._last_clicked_card = None
        self.selection_label.setVisible(False)

        self._update_count_label(len(ordered))

        for listing in ordered:
            card = ListingCard(listing)
            card.clicked.connect(self._on_card_clicked)
            card.select_box.stateChanged.connect(self._update_selection_label)
            item = QListWidgetItem()
            item.setSizeHint(card.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, card)
            self.cards.append(card)
            self._load_image(card)

        if self._show_hidden:
            for listing in self._hidden_listings:
                card = ListingCard(listing, is_hidden=True)
                item = QListWidgetItem()
                item.setSizeHint(card.sizeHint())
                self.list_widget.addItem(item)
                self.list_widget.setItemWidget(item, card)
                self._hidden_items.append(item)
                self._load_image(card)

    def _update_count_label(self, visible_count: int | None = None) -> None:
        if visible_count is None:
            visible_count = len(self.listings)
        text = f"{visible_count} listings" if visible_count else "No listings yet"
        if self._show_hidden and self._hidden_listings:
            text += f" + {len(self._hidden_listings)} hidden"
        self.count_label.setText(text)

    def _ignore_selected(self) -> None:
        selected = [card.listing for card in self.cards if card.is_selected()]
        if not selected:
            QMessageBox.information(
                self, "Nothing selected",
                "Tick the checkbox in the corner of the listings you want to ignore first.",
            )
            return

        for listing in selected:
            db.ignore_listing(self.db_connection, listing)

        ignored_ids = {listing.id for listing in selected}
        self.listings = [l for l in self.listings if l.id not in ignored_ids]
        existing_hidden_ids = {l.id for l in self._hidden_listings}
        self._hidden_listings.extend(l for l in selected if l.id not in existing_hidden_ids)
        self.toggle_hidden_button.setEnabled(bool(self._hidden_listings))
        self.statusBar().showMessage(
            f"Ignored {len(selected)} selected listings — they will no longer appear in search results", 6000
        )
        self._refresh_list()
        self.database_view.refresh()

    def _sort_for_display(self, listings: list[Listing]) -> list[Listing]:
        choice = self.local_sort_combo.currentData()
        if not choice:
            return list(listings)

        key, direction = choice.split("-", 1)
        if key == "price":
            def price_key(listing: Listing):
                value = numeric_price(listing.price)
                if value is None:
                    return (1, 0.0)
                return (0, -value if direction == "desc" else value)

            return sorted(listings, key=price_key)

        return sorted(listings, key=lambda listing: listing.date, reverse=(direction == "desc"))

    def _update_selection_label(self) -> None:
        n = sum(1 for card in self.cards if card.is_selected())
        if n:
            self.selection_label.setText(f"{n} selected")
            self.selection_label.setVisible(True)
        else:
            self.selection_label.setVisible(False)

    def _toggle_hidden_ads(self) -> None:
        self._show_hidden = not self._show_hidden
        self.toggle_hidden_button.setText(
            "Hide hidden ads" if self._show_hidden else "Show hidden ads"
        )
        self._refresh_list()

    def _on_card_clicked(self, card: ListingCard, shift: bool) -> None:
        if shift and self._last_clicked_card is not None and self._last_clicked_card in self.cards:
            a = self.cards.index(self._last_clicked_card)
            b = self.cards.index(card)
            lo, hi = min(a, b), max(a, b)
            for i in range(lo, hi + 1):
                self.cards[i].select_box.setChecked(True)
        else:
            self._last_clicked_card = card

    def _open_search_in_browser(self) -> None:
        if self._last_search_url:
            _open_url_safe(self._last_search_url)

    def _run_single_search(self, s) -> None:
        if self.thread is not None:
            self.statusBar().showMessage("A search is already running — wait for it to finish.", 4000)
            return

        kwargs = dict(
            query=s["query"],
            max_pages=s["pages"],
            delay=1.0,
            category_path=s["category_path"] or None,
            region_id=s["region_id"],
            city_id=s["city_id"],
            min_price=None,
            max_price=None,
            sort=s["sort"] or None,
        )

        base = (f"https://www.olx.pt/{s['category_path']}/"
                if s.get("category_path") else "https://www.olx.pt/ads/")
        url_params: dict = {"q": s["query"]}
        if s.get("region_id"):
            url_params["search[region_id]"] = s["region_id"]
        if s.get("city_id"):
            url_params["search[city_id]"] = s["city_id"]
        if s.get("sort"):
            url_params["search[order]"] = SORT_OPTIONS.get(s["sort"], s["sort"])
        self._last_search_url = base + "?" + urlencode(url_params)

        self.tabs.setCurrentWidget(self.search_page)
        self.search_button.setEnabled(False)
        self.search_button.setText("⏳")
        self.statusBar().showMessage(f"Scraping {s['query']!r}…")

        self.thread = QThread(self)
        self.worker = ScrapeWorker(kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._on_search_finished)
        self.worker.failed.connect(self._on_search_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_finished)
        self.thread.start()

    # -- Images -------------------------------------------------------------

    def _load_image(self, card: ListingCard) -> None:
        listing = card.listing
        if not listing.image_url:
            return

        cached = self.pixmap_cache.get(listing.id)
        if cached is not None:
            card.set_image(cached)
            return

        reply = self.network_manager.get(QNetworkRequest(QUrl(listing.image_url)))
        reply.finished.connect(lambda: self._on_image_loaded(reply, listing.id))

    def _on_image_loaded(self, reply: QNetworkReply, listing_id: str) -> None:
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return

        pixmap = QPixmap()
        if not pixmap.loadFromData(reply.readAll()):
            return

        cropped = cover_pixmap(pixmap, CARD_WIDTH, IMAGE_HEIGHT)
        self.pixmap_cache[listing_id] = cropped
        for card in self.cards:
            if card.listing.id == listing_id:
                card.set_image(cropped)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
