import logging
from collections import defaultdict
from csv import DictReader
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

from django.apps import apps
from django.utils import timezone
from django.utils.translation import gettext as _

import app
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from lists.models import CustomList

logger = logging.getLogger(__name__)

# GoodReads uses a 1-5 star rating; Yamtrack scores on a 0-10 scale.
GOODREADS_MIN_RATING = 1
GOODREADS_MAX_RATING = 5
GOODREADS_SCORE_SCALE = 2
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


class GoodReadsImporter:
    """Class to handle importing goodreads data from CSV files."""

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

        # Track existing media for "new" mode
        self.existing_media = helpers.get_existing_media(user)

        # Track existing media changed in "new" mode, by primary key
        self.to_update = defaultdict(dict)

        # Track media IDs to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))

        # Track bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        # Custom shelf name -> list, so one import does not create duplicates.
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

        logger.debug("processed %s", self.bulk_media)

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.to_update,
            helpers.EXISTING_MEDIA_UPDATE_FIELDS,
            self.user,
        )

        logger.debug("processed %s", self.bulk_media)

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

    def _process_row(self, row):
        """Process a single row from the CSV file."""
        # Skip early (before hitting the provider) when the shelf can't be
        # mapped to a Yamtrack status.
        if self._determine_status(row) is None:
            shelf = row["Exclusive Shelf"]
            self.warnings.append(
                f"{self._row_description(row)}: Unsupported shelf '{shelf}'.",
            )
            return

        default_source = Sources.HARDCOVER
        book = self._search_book(row, default_source)

        if not book:
            self.warnings.append(
                _(
                    "%(title)s: Couldn't find this book via Title or ISBN13"
                    " in %(source)s"
                )
                % {"title": row.get("Title"), "source": default_source.label}
            )
            return

        logger.debug("Found book %s", book)

        media_id = str(book["media_id"])

        # Existing books only get what they are missing
        existing = self._get_existing(media_id)
        if existing:
            item = existing.item
        else:
            item, _created = self._create_or_update_item(book)

        self._store_goodreads(item, row)
        self._assign_custom_shelves(item, row)

        if existing:
            if helpers.sync_existing_media(existing, self._get_values(row)):
                self.to_update[MediaTypes.BOOK.value][existing.pk] = existing
            return

        # Check if we should process this entry based on mode
        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            MediaTypes.BOOK.value,
            default_source.value,
            media_id,
            self.mode,
        ):
            return

        instance = self._create_media_instance(item, row)
        self.bulk_media[MediaTypes.BOOK.value].append(instance)

    def _get_existing(self, media_id):
        """Get the book already in Yamtrack, only tracked in "new" mode."""
        if self.mode != "new":
            return None

        return self.existing_media[MediaTypes.BOOK.value][Sources.HARDCOVER.value].get(
            media_id,
        )

    def _search_book(self, row, source):
        """Search Hardcover by cleaned ISBN13, then by title."""
        isbn = _clean_isbn(row.get("ISBN13"))
        if isbn:
            found = self._first_result(isbn, source)
            if found:
                return found

        title = (row.get("Title") or "").strip()
        if not title:
            return None
        return self._first_result(title, source)

    def _first_result(self, query, source):
        """Return the first Hardcover hit for a query."""
        results = services.search(
            MediaTypes.BOOK.value,
            query,
            1,
            source.value,
        ).get("results", [])
        if not results:
            return None
        return results[0]

    def _create_or_update_item(self, book):
        """Create or update the item in database."""
        media_type = MediaTypes.BOOK.value
        return app.models.Item.objects.update_or_create(
            media_id=book["media_id"],
            source=Sources.HARDCOVER.value,
            media_type=media_type,
            defaults={
                "title": book["title"],
                "image": book["image"],
            },
        )

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

    def _parse_average_rating(self, raw):
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

    def _parse_pages(self, raw):
        """Return a page count, or None when the cell is not a whole number."""
        if raw is None:
            return None
        text = str(raw).strip()
        if not text.isnumeric():
            return None
        return int(text)

    def _store_goodreads(self, item, row):
        """Save the Goodreads id, community average, and page count."""
        book_id = str(row.get("Book Id") or "").strip()[:32]
        average = self._parse_average_rating(row.get("Average Rating"))
        pages = self._parse_pages(row.get("Number of Pages"))
        if not book_id and average is None and pages is None:
            return

        rating, _created = app.models.ItemRating.objects.get_or_create(item=item)
        fields = []
        if book_id:
            rating.goodreads_id = book_id
            fields.append("goodreads_id")
        if average is not None:
            rating.goodreads_value = average
            rating.goodreads_normalized = _normalize_average(average)
            fields.extend(("goodreads_value", "goodreads_normalized"))
        if pages is not None:
            rating.page_count = pages
            fields.append("page_count")
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
        model = apps.get_model(app_label="app", model_name=MediaTypes.BOOK.value)

        # Parse dates
        date_created = self._parse_goodreads_date(row.get("Date Added", ""))
        date_rated = self._parse_goodreads_date(row.get("Date Read", ""))

        # filter out None dates
        dates = [date_created, date_rated]
        most_recent_date = max(date for date in dates if date)

        instance = model(item=item, user=self.user, **self._get_values(row))
        instance._history_date = most_recent_date or timezone.now()

        return instance


def _clean_isbn(value):
    """Strip Excel ``="..."`` wrapping from a Goodreads ISBN cell."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("="):
        text = text[1:].strip()
    return text.strip("\"'").strip()


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


def _normalize_average(value):
    """Scale a Goodreads 1-5 average onto 0-100."""
    return (value * Decimal(100) / Decimal(GOODREADS_MAX_RATING)).quantize(
        Decimal("0.1"),
    )
