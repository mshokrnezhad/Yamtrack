import logging
from collections import defaultdict
from csv import DictReader
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from app.models import Book, Item, ItemRating, MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from lists.models import CustomList

logger = logging.getLogger(__name__)

# GoodReads uses a 1-5 star rating; Yamtrack scores on a 0-10 scale.
GOODREADS_MIN_RATING = 1
GOODREADS_MAX_RATING = 5
GOODREADS_SCORE_SCALE = 2
RATING_COUNT_COLUMNS = ("Ratings Count", "ratings_count", "Number of Ratings")
# These shelves are a status, not a custom list.
SHELF_STATUS = {
    "read": Status.COMPLETED,
    "currently-reading": Status.IN_PROGRESS,
    "to-read": Status.PLANNING,
    "did-not-finish": Status.DROPPED,
}
EXCLUSIVE_SHELVES = frozenset(SHELF_STATUS)


def importer(file, user, mode):
    """Import media from CSV file using the class-based importer."""
    csv_importer = GoodReadsImporter(file, user, mode)
    return csv_importer.import_data()


def format_summary(stats):
    """Return the one-line count summary for a Goodreads command."""
    return (
        f"Updated {stats['updated']}. "
        f"Missing {stats['missing']}. "
        f"Repaired {stats['repaired']}. "
        f"Created {stats['created']}."
    )


def resolve_user(username):
    """Return the named user, or the earliest user when the name is blank."""
    user_model = get_user_model()
    if username:
        try:
            return user_model.objects.get(username=username)
        except user_model.DoesNotExist as error:
            message = f"No user named {username}."
            raise ValueError(message) from error
    user = user_model.objects.order_by("pk").first()
    if user is None:
        message = "No users exist."
        raise ValueError(message)
    return user


def apply_supplement(ratings_csv, user, progress_csv=None, covers_csv=None):
    """Apply ratings, progress, and cover CSVs. No network calls.

    Blank averages are skipped. Unknown Goodreads ids are counted as missing.
    An empty cover URL leaves the image already stored on the item.
    """
    stats = {"updated": 0, "missing": 0, "repaired": 0, "created": 0}
    _apply_ratings(_read_csv(ratings_csv), stats)
    if progress_csv:
        _apply_progress(_read_csv(progress_csv), user, stats)
    if covers_csv:
        _apply_covers(_read_csv(covers_csv), stats)
    return stats


class GoodReadsImporter:
    """Import Goodreads rows as Goodreads items. No catalog lookup."""

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode.

        Args:
            file: Uploaded CSV file object
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []
        self.stats = {"updated": 0, "missing": 0, "repaired": 0, "created": 0}
        self.existing_media = helpers.get_existing_media(user)
        self._index_goodreads_books()
        # Book ids already handled in this file, so a repeated row is not a
        # second book.
        self._handled = set()
        self.to_update = defaultdict(dict)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)
        self.custom_lists = {}

        logger.info(
            "Initialized GoodReads CSV importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all GoodReads data from the CSV file."""
        try:
            decoded_file = self.file.read().decode("utf-8")
        except UnicodeDecodeError as e:
            msg = _("Invalid file format. Please upload a CSV file.")
            raise MediaImportError(msg) from e

        reader = DictReader(StringIO(decoded_file, newline=""))

        for row in reader:
            try:
                self._process_row(row)
            except services.ProviderAPIError as error:
                self.stats["missing"] += 1
                row_description = self._row_description(row)
                logger.warning(
                    "Error processing Goodreads entry: %s - %s",
                    row_description,
                    error,
                )
                error_msg = _("Error processing entry: %(description)s - %(error)s") % {
                    "description": row_description,
                    "error": error,
                }
                self.warnings.append(error_msg)
                continue
            except Exception as error:
                error_msg = _("Error processing entry: %(row)s") % {"row": row}
                raise MediaImportUnexpectedError(error_msg) from error

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.to_update,
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }
        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages

    def _row_description(self, row):
        """Return a useful label for warnings without assuming Yamtrack fields."""
        title = row.get("Title")
        book_id = row.get("Book Id")

        if title and book_id:
            return f"{title} (Goodreads ID {book_id})"
        if title:
            return title
        if book_id:
            return f"Goodreads ID {book_id}"
        return str(row)

    def _index_goodreads_books(self):
        """Map this user's Goodreads ids to the book already imported."""
        self._books_by_goodreads = {}
        books = Book.objects.filter(user=self.user).select_related(
            "item__public_rating",
        )
        for book in books:
            rating = _rating_or_none(book.item)
            if rating is None or not rating.goodreads_id:
                continue
            self._books_by_goodreads[rating.goodreads_id] = book

    def _process_row(self, row):
        """Create or reuse the Goodreads item for this Book Id."""
        if self._determine_status(row) is None:
            shelf = row["Exclusive Shelf"]
            self.warnings.append(
                f"{self._row_description(row)}: Unsupported shelf '{shelf}'.",
            )
            return

        book_id = self._book_id(row)
        if not book_id:
            self.warnings.append(f"{self._row_description(row)}: Missing Book Id.")
            return

        with transaction.atomic():
            item = self._ensure_item(row, book_id)
            self._store_goodreads(item, row)
            self._assign_custom_shelves(item, row)

        if book_id in self._handled:
            return
        self._handled.add(book_id)
        self._import_book(item, row, book_id)

    def _ensure_item(self, row, book_id):
        """Return the Goodreads item for this Book Id, keeping its cover."""
        title = _row_title(row)
        item, created = Item.objects.get_or_create(
            media_id=book_id,
            source=Sources.GOODREADS.value,
            media_type=MediaTypes.BOOK.value,
            defaults={
                "title": title,
                "image": settings.IMG_NONE,
            },
        )
        if not created and item.title != title:
            item.title = title
            item.save(update_fields=["title"])
        return item

    def _import_book(self, item, row, book_id):
        """Create this user's book, or update the one already tracked."""
        existing = self._books_by_goodreads.get(book_id)
        if existing is not None and existing.item_id != item.id:
            self._move_book(existing, item, book_id)
            self.stats["repaired"] += 1
            if self.mode == "new" and helpers.sync_existing_media(
                existing,
                self._get_values(row),
            ):
                self.to_update[MediaTypes.BOOK.value][existing.pk] = existing
            if self.mode == "new":
                return

        if self.mode == "new":
            tracked = self._tracked_book(item, book_id, existing)
            if tracked is not None:
                self.stats["updated"] += 1
                if helpers.sync_existing_media(tracked, self._get_values(row)):
                    self.to_update[MediaTypes.BOOK.value][tracked.pk] = tracked
                return

        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            MediaTypes.BOOK.value,
            Sources.GOODREADS.value,
            book_id,
            self.mode,
        ):
            return

        self.bulk_media[MediaTypes.BOOK.value].append(
            self._create_media_instance(item, row),
        )
        self.stats["created"] += 1

    def _tracked_book(self, item, book_id, existing):
        """Return this user's book when it already points at ``item``."""
        tracked = self.existing_media[MediaTypes.BOOK.value][
            Sources.GOODREADS.value
        ].get(book_id)
        if tracked is not None:
            return tracked
        if existing is not None and existing.item_id == item.id:
            return existing
        return None

    def _move_book(self, book, item, book_id):
        """Point this user's book at the Goodreads item for the same Book Id."""
        old_item = book.item
        Book.objects.filter(pk=book.pk).update(item=item)
        book.item = item
        self.existing_media[MediaTypes.BOOK.value][old_item.source].pop(
            str(old_item.media_id),
            None,
        )
        self.existing_media[MediaTypes.BOOK.value][item.source][book_id] = book
        self._books_by_goodreads[book_id] = book

    def _determine_status(self, row):
        """Map a Goodreads exclusive shelf to a status, or None if unmapped."""
        status = SHELF_STATUS.get(row["Exclusive Shelf"])
        return status.value if status else None

    def _parse_rating(self, raw):
        """Parse a GoodReads 1-5 rating into Yamtrack's 0-10 score scale.

        GoodReads exports the rating as an integer ("5") in older files and as a
        decimal ("5.0") in newer ones. Empty, "0", out-of-range or malformed
        values map to None (no rating).
        """
        try:
            value = float(raw)
        except (ValueError, TypeError):
            return None

        if not GOODREADS_MIN_RATING <= value <= GOODREADS_MAX_RATING:
            return None

        # scale 1-5 onto 0-10, preserving half-stars (3.5 -> 7, 4.5 -> 9)
        return round(value * GOODREADS_SCORE_SCALE, 1)

    def _parse_goodreads_date(self, date_str):
        """Parse GoodReads date string (YYYY/MM/DD) into datetime object."""
        if not date_str:
            return None

        return datetime.strptime(date_str, "%Y/%m/%d").replace(
            hour=0,
            minute=0,
            second=0,
            tzinfo=timezone.get_current_timezone(),
        )

    def _parse_pages(self, raw):
        """Return a page count, or None when the cell is not a whole number."""
        return _parse_count(raw)

    def _store_goodreads(self, item, row):
        """Save the Goodreads id, average, pages, author, year, and ISBN."""
        book_id = self._book_id(row)
        ItemRating.objects.filter(goodreads_id=book_id).exclude(item=item).update(
            goodreads_id="",
        )
        rating, _created = ItemRating.objects.get_or_create(item=item)
        fields = []
        if book_id:
            rating.goodreads_id = book_id
            fields.append("goodreads_id")
        average = _parse_average_rating(row.get("Average Rating"))
        if average is not None:
            rating.goodreads_value = average
            rating.goodreads_normalized = _normalize_average(average)
            fields.extend(("goodreads_value", "goodreads_normalized"))
        count = _row_ratings_count(row)
        if count is not None:
            rating.goodreads_ratings_count = count
            fields.append("goodreads_ratings_count")
        pages = self._parse_pages(row.get("Number of Pages"))
        if pages is not None:
            rating.page_count = pages
            fields.append("page_count")
        author = (row.get("Author") or "").strip()
        if author:
            rating.author = author
            fields.append("author")
        year = _publication_year(row)
        if year is not None:
            rating.publication_year = year
            fields.append("publication_year")
        isbn = _row_isbn(row)
        if isbn:
            rating.isbn = isbn
            fields.append("isbn")
        if fields:
            rating.save(update_fields=fields)

    def _assign_custom_shelves(self, item, row):
        """Add the book to one list per shelf that is not an exclusive status."""
        for name in _custom_shelf_names(row.get("Bookshelves")):
            self._custom_list(name).items.add(item)

    def _custom_list(self, name):
        """Return the user's list with this exact name, creating it once."""
        custom_list = self.custom_lists.get(name)
        if custom_list is not None:
            return custom_list
        custom_list = (
            CustomList.objects.filter(owner=self.user, name=name).order_by("id").first()
        )
        if custom_list is None:
            custom_list = CustomList.objects.create(owner=self.user, name=name)
        self.custom_lists[name] = custom_list
        return custom_list

    def _get_values(self, row):
        """Get the values of the book tracked in the row."""
        book_status = self._determine_status(row)
        pages = self._parse_pages(row.get("Number of Pages"))
        book_progress = (
            pages if book_status == Status.COMPLETED.value and pages is not None else 0
        )

        return {
            "score": self._parse_rating(row["My Rating"]),
            "progress": book_progress,
            "status": book_status,
            "end_date": self._parse_goodreads_date(row.get("Date Read", "")),
            "notes": row["Private Notes"],
        }

    def _create_media_instance(self, item, row):
        """Create media instance with all parameters."""
        date_created = self._parse_goodreads_date(row.get("Date Added", ""))
        date_rated = self._parse_goodreads_date(row.get("Date Read", ""))
        present = [value for value in (date_created, date_rated) if value]

        instance = Book(item=item, user=self.user, **self._get_values(row))
        instance._history_date = max(present) if present else timezone.now()
        return instance

    def _book_id(self, row):
        """Return the Goodreads book id, truncated to the stored column."""
        return str(row.get("Book Id") or "").strip()[:32]


def _read_csv(path):
    """Read a CSV into row dicts. A leading BOM is ignored."""
    with Path(path).open(newline="", encoding="utf-8-sig") as file:
        return list(DictReader(file))


def _apply_ratings(rows, stats):
    """Store Goodreads averages and rating counts. Skip blank averages."""
    for row in rows:
        average = _parse_average_rating(row.get("average"))
        if average is None:
            continue
        book_id = str(row.get("book_id") or "").strip()
        if not book_id:
            stats["missing"] += 1
            continue
        rating = ItemRating.objects.filter(goodreads_id=book_id).order_by("id").first()
        if rating is None:
            stats["missing"] += 1
            continue
        rating.goodreads_value = average
        rating.goodreads_normalized = _normalize_average(average)
        fields = ["goodreads_value", "goodreads_normalized"]
        count = _parse_count(row.get("ratings_count"))
        if count is not None:
            rating.goodreads_ratings_count = count
            fields.append("goodreads_ratings_count")
        rating.save(update_fields=fields)
        stats["updated"] += 1


def _apply_progress(rows, user, stats):
    """Set pages read. Do not change status, and do not call a provider."""
    for row in rows:
        book_id = str(row.get("book_id") or "").strip()
        pages = _parse_count(row.get("pages_read"))
        if not book_id or pages is None:
            stats["missing"] += 1
            continue
        rating = ItemRating.objects.filter(goodreads_id=book_id).order_by("id").first()
        if rating is None:
            stats["missing"] += 1
            continue
        book = (
            Book.objects.filter(user=user, item=rating.item)
            .order_by("-created_at")
            .first()
        )
        if book is None:
            stats["missing"] += 1
            continue
        Book.objects.filter(pk=book.pk).update(progress=pages)
        total = _parse_count(row.get("total_pages"))
        if total and not rating.page_count:
            rating.page_count = total
            rating.save(update_fields=["page_count"])
        stats["updated"] += 1


def _apply_covers(rows, stats):
    """Set cover URLs on Goodreads items. Do not download the files."""
    for row in rows:
        book_id = str(row.get("book_id") or "").strip()
        url = str(row.get("cover_url") or "").strip()
        if not book_id:
            stats["missing"] += 1
            continue
        rating = (
            ItemRating.objects.filter(goodreads_id=book_id)
            .select_related("item")
            .order_by("id")
            .first()
        )
        if rating is None or rating.item.source != Sources.GOODREADS.value:
            stats["missing"] += 1
            continue
        if not url:
            continue
        item = rating.item
        if item.image != url:
            item.image = url
            item.save(update_fields=["image"])
        stats["updated"] += 1


def _rating_or_none(item):
    """Return the item's rating row, or None when it has none."""
    try:
        return item.public_rating
    except ItemRating.DoesNotExist:
        return None


def _clean_isbn(value):
    """Strip Excel ``="..."`` wrapping from a Goodreads ISBN cell."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("="):
        text = text[1:].strip()
    return text.strip("\"'").strip()


def _row_isbn(row):
    """Return the cleaned ISBN13, or the cleaned ISBN when ISBN13 is empty."""
    isbn13 = _clean_isbn(row.get("ISBN13"))
    if isbn13:
        return isbn13
    return _clean_isbn(row.get("ISBN"))


def _row_title(row):
    """Return the CSV title exactly, or Unknown when the cell is blank."""
    title = row.get("Title")
    if title is None or not str(title).strip():
        return "Unknown"
    return str(title)


def _publication_year(row):
    """Return Year Published, or Original Publication Year when that is empty."""
    year = _parse_year(row.get("Year Published"))
    if year is not None:
        return year
    return _parse_year(row.get("Original Publication Year"))


def _parse_year(raw):
    """Return a publication year, or None when the cell is not a whole number."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.isnumeric():
        return None
    return int(text)


def _custom_shelf_names(raw):
    """Return shelf names that should become custom lists, in file order."""
    names = []
    seen = set()
    for part in str(raw or "").split(","):
        name = part.strip()
        if not name or name in EXCLUSIVE_SHELVES or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _parse_average_rating(raw):
    """Return a Goodreads 1-5 community average, or None when unusable."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value < 0 or value > GOODREADS_MAX_RATING:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_count(raw):
    """Return a non-negative integer, or None when the cell is not one."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text.isnumeric():
        return None
    return int(text)


def _row_ratings_count(row):
    """Return a ratings-count cell when the export has one."""
    for key in RATING_COUNT_COLUMNS:
        if key in row:
            return _parse_count(row.get(key))
    return None


def _normalize_average(value):
    """Scale a Goodreads 1-5 average onto 0-100."""
    return (value * Decimal(100) / Decimal(GOODREADS_MAX_RATING)).quantize(
        Decimal("0.1"),
    )
