import logging
import re
import unicodedata
from collections import defaultdict
from csv import DictReader
from datetime import datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from io import StringIO
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from app.models import Book, Item, ItemRating, MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from lists.models import CustomList, CustomListItem

logger = logging.getLogger(__name__)

# GoodReads uses a 1-5 star rating; Yamtrack scores on a 0-10 scale.
GOODREADS_MIN_RATING = 1
GOODREADS_MAX_RATING = 5
GOODREADS_SCORE_SCALE = 2
# ponytail: ratio/token overlap misses translations that are not the same
# string. An ISBN hit bypasses this; otherwise the row becomes a manual item.
TITLE_MATCH_RATIO = 0.82
TITLE_TOKEN_OVERLAP = 0.8
RATING_COUNT_COLUMNS = ("Ratings Count", "ratings_count", "Number of Ratings")
GOODREADS_RATING_FIELDS = (
    "goodreads_id",
    "goodreads_value",
    "goodreads_normalized",
    "goodreads_ratings_count",
    "page_count",
)
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


def apply_supplement(ratings_csv, user, progress_csv=None):
    """Apply a ratings CSV and an optional currently-reading progress CSV.

    No network calls. Blank averages are skipped. Unknown Goodreads ids are
    counted as missing.
    """
    stats = {"updated": 0, "missing": 0, "repaired": 0, "created": 0}
    _apply_ratings(_read_csv(ratings_csv), stats)
    if progress_csv:
        _apply_progress(_read_csv(progress_csv), user, stats)
    return stats


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
        self.stats = {"updated": 0, "missing": 0, "repaired": 0, "created": 0}

        # Track existing media for "new" mode
        self.existing_media = helpers.get_existing_media(user)
        self._index_goodreads_books()
        # source + media id -> Goodreads id, including rows earlier in this file.
        self._index_claims()

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

    def _index_goodreads_books(self):
        """Map this user's Goodreads ids to the book already imported."""
        self._goodreads_items = {}
        self._books_by_goodreads = {}
        books = Book.objects.filter(user=self.user).select_related(
            "item__public_rating",
        )
        for book in books:
            rating = _rating_or_none(book.item)
            if rating is None or not rating.goodreads_id:
                continue
            self._goodreads_items[rating.goodreads_id] = book.item
            self._books_by_goodreads.setdefault(rating.goodreads_id, book)

    def _index_claims(self):
        """Map each catalog item to the Goodreads id already stored on it."""
        self._claims = {}
        claimed = ItemRating.objects.exclude(goodreads_id="").values_list(
            "item__source",
            "item__media_id",
            "goodreads_id",
        )
        for source, media_id, goodreads_id in claimed:
            self._claims[_claim_key(source, media_id)] = goodreads_id

    def _claim_owner(self, source, media_id):
        """Return the Goodreads id that owns this item, or an empty string."""
        key = _claim_key(source, media_id)
        if key in self._claims:
            return self._claims[key]
        owner = (
            ItemRating.objects.filter(
                item__source=source,
                item__media_id=str(media_id),
                item__media_type=MediaTypes.BOOK.value,
            )
            .exclude(goodreads_id="")
            .values_list("goodreads_id", flat=True)
            .first()
        )
        self._claims[key] = owner or ""
        return self._claims[key]

    def _claimed_by_other(self, book, row):
        """Return whether a different Goodreads id already uses this item."""
        owner = self._claim_owner(book.get("source"), book.get("media_id"))
        return bool(owner) and owner != self._book_id(row)

    def _claim_item(self, source, media_id, book_id):
        """Remember that this Goodreads id is using the item.

        A later row in the same file consults this before its rating is saved.
        An id already stored for a different book is left in place.
        """
        if not book_id:
            return
        key = _claim_key(source, media_id)
        owner = self._claims.get(key)
        if owner and owner != book_id:
            return
        self._claims[key] = book_id

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

        book = dict(self._search_book(row))
        book["source"] = book.get("source") or Sources.HARDCOVER.value
        book["media_id"] = str(book["media_id"])
        book["image"] = book.get("image") or settings.IMG_NONE

        if self._repair_mismatch(book, row):
            return

        self._import_resolved(self._reuse_matching_item(book, row), row)

    def _repair_mismatch(self, book, row):
        """Move a mismatched Goodreads id onto the resolved item.

        An ISBN hit whose catalog title is another language shares the media
        id, so it is not treated as a mismatch. Open Library and manual items
        are retitled from the CSV instead of moved. The old catalog item is
        left unchanged: it may be a different work.
        """
        stored = self._goodreads_items.get(self._book_id(row))
        if stored is None or _same_item(stored, book):
            return False
        if self._keep_stored(stored, row):
            return False
        self._move_user_book(stored, book, row)
        return True

    def _keep_stored(self, stored, row):
        """Return whether this Goodreads id should stay on its current item.

        Open Library and manual items keep the CSV title, so a romanized
        catalog title is not a reason to leave them. A Hardcover item stays
        when its title still matches the CSV.
        """
        if stored.source in (Sources.OPENLIBRARY.value, Sources.MANUAL.value):
            return True
        return _titles_match(row.get("Title"), stored.title)

    def _reuse_matching_item(self, book, row):
        """Keep the item already imported for this Goodreads id."""
        stored = self._goodreads_items.get(self._book_id(row))
        if stored is None or _same_item(stored, book):
            return book
        if not self._keep_stored(stored, row):
            return book
        return self._with_csv_title(
            {
                "media_id": stored.media_id,
                "source": stored.source,
                "title": stored.title,
                "image": stored.image,
            },
            row,
        )

    def _move_user_book(self, old_item, book, row):
        """Move this user's book, Goodreads fields, and lists onto ``book``."""
        book_id = self._book_id(row)
        with transaction.atomic():
            new_item, _created = self._create_or_update_item(book)
            self._move_rating(old_item, new_item)
            self._move_lists(old_item, new_item)
            moved = self._move_books(old_item, new_item, book_id)
            if self._store_goodreads(new_item, row) is False:
                return
            self._retitle_from_csv(new_item, row)
            self._assign_custom_shelves(new_item, row)

        self.stats["repaired"] += 1
        logger.info(
            "Repaired Goodreads %s for %s onto item %s",
            book_id,
            self.user.username,
            new_item.pk,
        )
        if moved is None:
            return
        if helpers.sync_existing_media(moved, self._get_values(row)):
            self.to_update[MediaTypes.BOOK.value][moved.pk] = moved

    def _move_books(self, old_item, new_item, book_id):
        """Point this user's books at the replacement item."""
        books = list(Book.objects.filter(user=self.user, item=old_item))
        if not books:
            return None
        Book.objects.filter(pk__in=[book.pk for book in books]).update(item=new_item)
        old_bucket = self.existing_media[MediaTypes.BOOK.value][old_item.source]
        old_bucket.pop(str(old_item.media_id), None)
        for book in books:
            book.item = new_item
        kept = self._books_by_goodreads.get(book_id) or books[0]
        kept.item = new_item
        source = new_item.source
        media_id = str(new_item.media_id)
        self.existing_media[MediaTypes.BOOK.value][source][media_id] = kept
        self._goodreads_items[book_id] = new_item
        self._books_by_goodreads[book_id] = kept
        return kept

    def _move_rating(self, old_item, new_item):
        """Copy Goodreads fields off the mismatched item."""
        old = _rating_or_none(old_item)
        if old is None:
            return
        new, _created = ItemRating.objects.get_or_create(item=new_item)
        for field in GOODREADS_RATING_FIELDS:
            setattr(new, field, getattr(old, field))
        new.save(update_fields=list(GOODREADS_RATING_FIELDS))
        moved_id = old.goodreads_id
        old.goodreads_id = ""
        old.goodreads_value = None
        old.goodreads_normalized = None
        old.goodreads_ratings_count = None
        old.page_count = None
        old.save(update_fields=list(GOODREADS_RATING_FIELDS))
        self._claims[_claim_key(old_item.source, old_item.media_id)] = ""
        if moved_id:
            self._claims[_claim_key(new_item.source, new_item.media_id)] = moved_id

    def _move_lists(self, old_item, new_item):
        """Move this user's shelf memberships onto the replacement item."""
        memberships = CustomListItem.objects.filter(
            item=old_item,
            custom_list__owner=self.user,
        )
        for membership in memberships:
            already = CustomListItem.objects.filter(
                item=new_item,
                custom_list_id=membership.custom_list_id,
            ).exists()
            if already:
                membership.delete()
                continue
            membership.item = new_item
            membership.save(update_fields=["item"])

    def _import_resolved(self, book, row):
        """Create or update the book from a resolved catalog or manual item."""
        source = book["source"]
        media_id = str(book["media_id"])
        book_id = self._book_id(row)
        # Claim before the rating row is saved so the next CSV row cannot
        # take this item.
        self._claim_item(source, media_id, book_id)
        existing = self._get_existing(media_id, source, book_id, row.get("Title"))
        if existing:
            item = existing.item
        else:
            item, _created = self._create_or_update_item(book)

        if self._store_goodreads(item, row) is False:
            return
        self._retitle_from_csv(item, row)
        self._assign_custom_shelves(item, row)

        if existing:
            self.stats["updated"] += 1
            if helpers.sync_existing_media(existing, self._get_values(row)):
                self.to_update[MediaTypes.BOOK.value][existing.pk] = existing
            return

        # Check if we should process this entry based on mode
        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            MediaTypes.BOOK.value,
            source,
            media_id,
            self.mode,
        ):
            return

        instance = self._create_media_instance(item, row)
        self.bulk_media[MediaTypes.BOOK.value].append(instance)
        self.stats["created"] += 1

    def _get_existing(self, media_id, source, book_id, csv_title):
        """Get the book already in Yamtrack, only tracked in "new" mode.

        A Goodreads id matches even when an earlier import used another source.
        A stored title that is a different work is not reused here.
        """
        if self.mode != "new":
            return None

        existing = self.existing_media[MediaTypes.BOOK.value][source].get(
            str(media_id),
        )
        if existing is not None:
            return existing

        book = self._books_by_goodreads.get(book_id)
        if book is None or not _titles_match(csv_title, book.item.title):
            return None
        return book

    def _search_book(self, row, source=Sources.HARDCOVER):
        """Resolve a row without letting two Goodreads ids share an item.

        ISBN hits are trusted, including a Hardcover title in another language.
        A title-only hit is kept only when it matches the CSV title. A catalog
        item already used by a different Goodreads id is skipped: Hardcover
        falls through to Open Library, and a claimed or missing Open Library
        hit becomes a manual item. Open Library titles come from the CSV.
        """
        found = self._search_catalog(row, source)
        if found is not None and not self._claimed_by_other(found, row):
            return self._with_csv_title(found, row)
        if found is not None and found.get("source") == Sources.HARDCOVER.value:
            library = self._search_openlibrary(row)
            if library is not None and not self._claimed_by_other(library, row):
                return self._with_csv_title(library, row)
        return self._manual_book((row.get("Title") or "").strip())

    def _search_openlibrary(self, row):
        """Search Open Library by ISBN, then by title."""
        providers = [Sources.OPENLIBRARY]
        isbn = _clean_isbn(row.get("ISBN13"))
        title = (row.get("Title") or "").strip()
        if isbn:
            hit = self._isbn_hit(isbn, providers)
            if hit is not None:
                return hit
        if not title:
            return None
        author = (row.get("Author") or "").strip()
        return self._title_hit(title, author, providers)

    def _with_csv_title(self, book, row):
        """Use the CSV title for Open Library and manual items."""
        if book.get("source") not in (
            Sources.OPENLIBRARY.value,
            Sources.MANUAL.value,
        ):
            return book
        title = (row.get("Title") or "").strip()
        if title:
            book["title"] = title
        return book

    def _retitle_from_csv(self, item, row):
        """Store the CSV title on an Open Library or manual item."""
        if item.source not in (Sources.OPENLIBRARY.value, Sources.MANUAL.value):
            return
        title = (row.get("Title") or "").strip()
        if not title or item.title == title:
            return
        item.title = title
        item.save(update_fields=["title"])

    def _search_catalog(self, row, source):
        """Search catalogs. None means the caller should create a manual item."""
        providers = _provider_order(source)
        isbn = _clean_isbn(row.get("ISBN13"))
        title = (row.get("Title") or "").strip()
        if isbn:
            hit = self._isbn_hit(isbn, providers)
            if hit is not None:
                return hit
        if not title:
            return None
        author = (row.get("Author") or "").strip()
        return self._title_hit(title, author, providers)

    def _isbn_hit(self, isbn, providers):
        """Return the first ISBN hit. The catalog title is not compared."""
        for provider in providers:
            found = self._first_result(isbn, provider)
            if found is not None:
                return found
        return None

    def _title_hit(self, title, author, providers):
        """Return the first title search whose title matches the CSV title."""
        for provider in providers:
            for query in _title_queries(title, author):
                found = self._first_result(query, provider)
                if found is not None and _titles_match(title, found.get("title")):
                    return found
        return None

    def _manual_book(self, title):
        """Build an unsaved manual book using the CSV title."""
        return {
            "media_id": Item.generate_manual_id(),
            "source": Sources.MANUAL.value,
            "media_type": MediaTypes.BOOK.value,
            "title": title or "Unknown",
            "image": settings.IMG_NONE,
        }

    def _first_result(self, query, source):
        """Return the first hit for a query, stamped with the source we asked."""
        results = services.search(
            MediaTypes.BOOK.value,
            query,
            1,
            source.value,
        ).get("results", [])
        if not results:
            return None
        hit = dict(results[0])
        hit["source"] = hit.get("source") or source.value
        hit["media_id"] = str(hit["media_id"])
        hit["image"] = hit.get("image") or settings.IMG_NONE
        return hit

    def _create_or_update_item(self, book):
        """Create or update the item in database."""
        source = book.get("source") or Sources.HARDCOVER.value
        return Item.objects.update_or_create(
            media_id=str(book["media_id"]),
            source=source,
            media_type=MediaTypes.BOOK.value,
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

    def _parse_pages(self, raw):
        """Return a page count, or None when the cell is not a whole number."""
        return _parse_count(raw)

    def _store_goodreads(self, item, row):
        """Save the Goodreads id, community average, rating count, and pages.

        Return False when another Goodreads id already owns the item, so the
        caller does not add shelves or a book onto it.
        """
        book_id = self._book_id(row)
        average = _parse_average_rating(row.get("Average Rating"))
        pages = self._parse_pages(row.get("Number of Pages"))
        count = _row_ratings_count(row)
        if not book_id and average is None and pages is None and count is None:
            return True

        rating, _created = ItemRating.objects.get_or_create(item=item)
        fields = []
        if book_id and rating.goodreads_id and rating.goodreads_id != book_id:
            return False
        if book_id:
            rating.goodreads_id = book_id
            fields.append("goodreads_id")
            self._claim_item(item.source, item.media_id, book_id)
        if average is not None:
            rating.goodreads_value = average
            rating.goodreads_normalized = _normalize_average(average)
            fields.extend(("goodreads_value", "goodreads_normalized"))
        if count is not None:
            rating.goodreads_ratings_count = count
            fields.append("goodreads_ratings_count")
        if pages is not None:
            rating.page_count = pages
            fields.append("page_count")
        if fields:
            rating.save(update_fields=fields)
        return True

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


def _provider_order(source):
    """Search the requested source first, then the other book catalog."""
    order = []
    for provider in (source, Sources.HARDCOVER, Sources.OPENLIBRARY):
        if provider not in order:
            order.append(provider)
    return order


def _title_queries(title, author):
    """Return the title, then title plus author when an author is present."""
    queries = [title]
    if author:
        queries.append(f"{title} {author}")
    return queries


def _same_item(item, book):
    """Return whether ``item`` is the catalog row ``book`` resolved to."""
    return item.source == book.get("source") and item.media_id == str(
        book.get("media_id"),
    )


def _claim_key(source, media_id):
    """Return the in-memory key for one catalog item."""
    return (source, str(media_id))


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


def _normalize_title(title):
    """Lowercase a title and drop punctuation and series parentheses."""
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = text.casefold()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def _titles_match(left, right):
    """Return whether two titles are the same work, not merely related.

    A match is an equal or contained normalized title, a high
    SequenceMatcher ratio, or a high token overlap. A clearly different
    work, such as another book that only shares a word, does not match.
    """
    normalized_left = _normalize_title(left)
    normalized_right = _normalize_title(right)
    if not normalized_left or not normalized_right:
        return False
    if (
        normalized_left == normalized_right
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    ):
        return True
    ratio = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    if ratio >= TITLE_MATCH_RATIO:
        return True
    left_tokens = normalized_left.split()
    right_tokens = normalized_right.split()
    shorter, longer = (
        (left_tokens, set(right_tokens))
        if len(left_tokens) <= len(right_tokens)
        else (right_tokens, set(left_tokens))
    )
    if not shorter:
        return False
    overlap = sum(token in longer for token in shorter) / len(shorter)
    return overlap >= TITLE_TOKEN_OVERLAP
