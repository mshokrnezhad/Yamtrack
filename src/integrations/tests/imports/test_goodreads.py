from csv import DictReader, DictWriter
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Book,
    Item,
    ItemRating,
    MediaTypes,
    Sources,
    Status,
)
from app.providers import services
from events.models import Event
from integrations.imports import (
    goodreads,
)
from lists.models import CustomList, CustomListItem

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportGoodreads(TestCase):
    """Test importing media from GoodReads CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_goodreads.csv").open("rb") as file:
            self.import_results = goodreads.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported books."""
        self.assertEqual(Book.objects.filter(user=self.user).count(), 3)

    def test_historical_records(self):
        """Test historical records creation during import."""
        book = Book.objects.filter(user=self.user).first()
        self.assertEqual(book.history.count(), 1)

    def test_stored_progress(self):
        """Test progress of imported books."""
        read_book = Book.objects.get(status=Status.COMPLETED.value)
        self.assertEqual(read_book.status, Status.COMPLETED.value)
        self.assertEqual(read_book.progress, 994)

        read_book = Book.objects.get(status=Status.IN_PROGRESS.value)
        self.assertEqual(read_book.status, Status.IN_PROGRESS.value)
        self.assertEqual(read_book.progress, 0)

    def test_decimal_rating_scored(self):
        """Test a modern decimal rating ("5.0") maps to the 0-10 scale."""
        read_book = Book.objects.get(status=Status.COMPLETED.value)
        self.assertEqual(read_book.score, 10)

    def test_stores_goodreads_fields_from_the_mock_file(self):
        """The sample export stores id, average, and pages, and no shelf lists."""
        ghosts = Item.objects.get(public_rating__goodreads_id="28825810")
        self.assertEqual(ghosts.public_rating.goodreads_value, Decimal("4.29"))
        self.assertEqual(ghosts.public_rating.goodreads_normalized, Decimal("85.8"))
        self.assertEqual(ghosts.public_rating.page_count, 90)
        self.assertIsNone(ghosts.public_rating.metacritic_value)

        wise = Item.objects.get(public_rating__goodreads_id="1215032")
        self.assertEqual(wise.public_rating.goodreads_value, Decimal("4.55"))
        self.assertEqual(wise.public_rating.page_count, 994)
        sand = Item.objects.get(public_rating__goodreads_id="39298848")
        self.assertEqual(sand.public_rating.goodreads_value, Decimal("3.64"))
        self.assertEqual(sand.public_rating.page_count, 160)
        self.assertFalse(CustomList.objects.filter(owner=self.user).exists())

    def test_items_are_goodreads_editions(self):
        """Each Book Id is its own Goodreads item, titled from the CSV."""
        ghosts = Item.objects.get(media_id="28825810")
        wise = Item.objects.get(media_id="1215032")
        sand = Item.objects.get(media_id="39298848")
        self.assertEqual(ghosts.source, Sources.GOODREADS.value)
        self.assertEqual(
            ghosts.title,
            "Ghosts of the Tristan Basin (Powder Mage, #0.8)",
        )
        self.assertEqual(ghosts.image, settings.IMG_NONE)
        self.assertEqual(ghosts.public_rating.author, "Brian  McClellan")
        self.assertEqual(ghosts.public_rating.publication_year, 2016)
        self.assertEqual(ghosts.public_rating.isbn, "")
        self.assertEqual(wise.source, Sources.GOODREADS.value)
        self.assertEqual(wise.public_rating.author, "Patrick Rothfuss")
        self.assertEqual(wise.public_rating.publication_year, 2011)
        self.assertEqual(wise.public_rating.isbn, "9780756404734")
        self.assertEqual(sand.source, Sources.GOODREADS.value)
        self.assertEqual(sand.public_rating.author, "Brandon Sanderson")
        self.assertEqual(sand.public_rating.publication_year, 2019)
        self.assertEqual(sand.public_rating.isbn, "")
        self.assertEqual(Item.objects.count(), 3)


class ImportGoodreadsMultilineNotes(TestCase):
    """Test importing GoodReads rows whose quoted fields span several lines."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def test_multiline_notes_are_preserved(self):
        """Test that quoted multiline notes keep embedded newlines."""
        with Path(mock_path / "import_goodreads.csv").open(newline="") as file:
            reader = DictReader(file)
            row = next(row for row in reader if row["Exclusive Shelf"] == "read")
        row["Private Notes"] = "line one\r\nline two"

        buffer = StringIO(newline="")
        writer = DictWriter(buffer, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerow(row)

        goodreads.importer(BytesIO(buffer.getvalue().encode()), self.user, "new")

        book = Book.objects.get(user=self.user)
        self.assertEqual(book.notes, "line one\r\nline two")


class ParseRating(TestCase):
    """Test mapping of Goodreads 1-5 ratings onto Yamtrack's 0-10 scale."""

    def setUp(self):
        """Create user and importer for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.importer = goodreads.GoodReadsImporter(None, self.user, "new")

    def test_integer_and_decimal_ratings(self):
        """Test both old ("5") and modern ("5.0") export formats scale by 2."""
        self.assertEqual(self.importer._parse_rating("5"), 10)
        self.assertEqual(self.importer._parse_rating("5.0"), 10)
        self.assertEqual(self.importer._parse_rating("3"), 6)
        self.assertEqual(self.importer._parse_rating("3.0"), 6)

    def test_half_star_scales_linearly(self):
        """Test half-star ratings scale linearly (3.5 -> 7, 4.5 -> 9)."""
        self.assertEqual(self.importer._parse_rating("3.5"), 7)
        self.assertEqual(self.importer._parse_rating("4.5"), 9)

    def test_no_rating_returns_none(self):
        """Test empty, zero and missing ratings map to no score."""
        self.assertIsNone(self.importer._parse_rating("0"))
        self.assertIsNone(self.importer._parse_rating("0.0"))
        self.assertIsNone(self.importer._parse_rating(""))
        self.assertIsNone(self.importer._parse_rating(None))

    def test_out_of_range_and_garbage_returns_none(self):
        """Test out-of-range and malformed ratings map to no score."""
        self.assertIsNone(self.importer._parse_rating("10.0"))
        self.assertIsNone(self.importer._parse_rating("6"))
        self.assertIsNone(self.importer._parse_rating("5,0"))
        self.assertIsNone(self.importer._parse_rating("abc"))


class DetermineStatus(TestCase):
    """Test mapping of Goodreads exclusive shelves to Yamtrack statuses."""

    def setUp(self):
        """Create user and importer for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.importer = goodreads.GoodReadsImporter(None, self.user, "new")

    def test_known_shelves(self):
        """Test the built-in Goodreads exclusive shelves map correctly."""
        cases = {
            "read": Status.COMPLETED,
            "currently-reading": Status.IN_PROGRESS,
            "to-read": Status.PLANNING,
            "did-not-finish": Status.DROPPED,
        }
        for shelf, status in cases.items():
            row = {"Exclusive Shelf": shelf}
            self.assertEqual(self.importer._determine_status(row), status.value)

    def test_custom_shelf_returns_none(self):
        """Test unknown/custom shelves are unmapped."""
        row = {"Exclusive Shelf": "fitness"}
        self.assertIsNone(self.importer._determine_status(row))

    def test_custom_shelf_row_is_skipped_with_warning(self):
        """Test a row on an unsupported shelf is skipped, not imported."""
        row = {"Exclusive Shelf": "fitness", "Title": "Pain Free", "Book Id": "252465"}
        self.importer._process_row(row)
        self.assertEqual(self.importer.bulk_media, {})
        self.assertEqual(len(self.importer.warnings), 1)
        self.assertIn("Unsupported shelf 'fitness'", self.importer.warnings[0])


class ImportGoodreadsProviderErrors(TestCase):
    """Test GoodReads provider error handling."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    @patch("integrations.imports.goodreads.GoodReadsImporter._process_row")
    def test_provider_error_warns_with_goodreads_fields(self, mock_process_row):
        """Test provider failures warn and continue without requiring media_id."""
        response = MagicMock(status_code=408, text='{"error":"Request timeout"}')
        error = requests.exceptions.HTTPError(response=response)
        mock_process_row.side_effect = services.ProviderAPIError(
            Sources.HARDCOVER.value,
            error,
        )

        with Path(mock_path / "import_goodreads.csv").open("rb") as file:
            imported_counts, warnings = goodreads.importer(file, self.user, "new")

        self.assertEqual(imported_counts, {})
        self.assertIn(
            "Ghosts of the Tristan Basin (Powder Mage, #0.8) (Goodreads ID 28825810)",
            warnings,
        )
        self.assertIn("There was an error contacting the Hardcover API", warnings)


class ImportGoodreadsNewMode(TestCase):
    """Test that "new" mode only imports what is missing from Yamtrack."""

    def setUp(self):
        """Create user and an existing planned book for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        item = Item.objects.create(
            media_id="1215032",
            source=Sources.GOODREADS.value,
            media_type=MediaTypes.BOOK.value,
            title="The Wise Man's Fear",
            image="https://example.com/image.jpg",
        )
        Book.objects.bulk_create(
            [Book(item=item, user=self.user, status=Status.PLANNING.value)],
        )

    def run_import(self, **fields):
        """Run a GoodReads import in "new" mode of the existing book."""
        with Path(mock_path / "import_goodreads.csv").open(newline="") as file:
            reader = DictReader(file)
            row = next(row for row in reader if row["Exclusive Shelf"] == "read")
        row.update(fields)

        buffer = StringIO(newline="")
        writer = DictWriter(buffer, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerow(row)

        return goodreads.importer(
            BytesIO(buffer.getvalue().encode()),
            self.user,
            "new",
        )

    def test_read_book_is_completed(self):
        """A planned book read on GoodReads is completed with its rating."""
        imported_counts, _ = self.run_import(
            **{"My Rating": "4", "Number of Pages": "994", "Date Read": "2024/05/01"},
        )

        book = Book.objects.get(user=self.user)
        self.assertEqual(imported_counts, {})
        self.assertEqual(book.status, Status.COMPLETED.value)
        self.assertEqual(book.score, 8)
        self.assertEqual(book.progress, 994)
        self.assertEqual(timezone.localdate(book.end_date), date(2024, 5, 1))

    def test_existing_values_are_kept(self):
        """The status and score tracked in Yamtrack are not overwritten."""
        Book.objects.filter(user=self.user).update(
            status=Status.PAUSED.value,
            score=5,
        )

        self.run_import(**{"My Rating": "4"})

        book = Book.objects.get(user=self.user)
        self.assertEqual(book.status, Status.PAUSED.value)
        self.assertEqual(book.score, 5)
        self.assertIsNone(book.end_date)


def _csv_file(rows, fieldnames=None):
    """Build an in-memory Goodreads CSV."""
    if fieldnames is None:
        fieldnames = list(rows[0])
    buffer = StringIO(newline="")
    writer = DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return BytesIO(buffer.getvalue().encode())


def _row(**fields):
    """Return one Goodreads row, with the columns the importer reads."""
    base = {
        "Book Id": "1",
        "Title": "Sample",
        "ISBN13": '=""',
        "My Rating": "0",
        "Average Rating": "4.29",
        "Number of Pages": "100",
        "Date Read": "",
        "Date Added": "2024/01/01",
        "Bookshelves": "",
        "Exclusive Shelf": "to-read",
        "Private Notes": "",
    }
    base.update(fields)
    return base


class CleanIsbn(TestCase):
    """Excel formula wrapping around ISBN cells."""

    def test_strips_formula_wrapping(self):
        """Wrapped, empty, and plain ISBNs become a bare value or nothing."""
        self.assertEqual(goodreads._clean_isbn('="9789644480645"'), "9789644480645")
        self.assertEqual(goodreads._clean_isbn('="9780756404734"'), "9780756404734")
        self.assertEqual(goodreads._clean_isbn('=""'), "")
        self.assertEqual(goodreads._clean_isbn("9789644480645"), "9789644480645")
        self.assertEqual(goodreads._clean_isbn(""), "")
        self.assertEqual(goodreads._clean_isbn(None), "")

    @patch("app.providers.openlibrary.book")
    @patch("app.providers.hardcover.book")
    @patch("integrations.imports.goodreads.services.search")
    def test_wrapped_isbn_is_stored_without_a_catalog_search(
        self,
        search,
        hardcover,
        library,
    ):
        """Excel wrapping is stripped onto the Goodreads item. No catalog call."""
        self.user = get_user_model().objects.create_user(
            username="wrap",
            password="12345",  # noqa: S106
        )
        row = _row(
            Title="Wrapped",
            Author="Wrapped Author",
            ISBN='="9644480645"',
            ISBN13='="9789644480645"',
            **{
                "Exclusive Shelf": "to-read",
                "Book Id": "99",
                "Year Published": "",
                "Original Publication Year": "2001",
            },
        )

        goodreads.importer(_csv_file([row]), self.user, "new")

        search.assert_not_called()
        hardcover.assert_not_called()
        library.assert_not_called()
        item = Item.objects.get()
        self.assertEqual(item.source, Sources.GOODREADS.value)
        self.assertEqual(item.media_id, "99")
        self.assertEqual(item.title, "Wrapped")
        self.assertEqual(item.public_rating.isbn, "9789644480645")
        self.assertEqual(item.public_rating.author, "Wrapped Author")
        self.assertEqual(item.public_rating.publication_year, 2001)


class ImportGoodreadsMetadata(TestCase):
    """Goodreads id, community average, page count, and custom shelves."""

    def setUp(self):
        """Create a user for the import."""
        self.user = get_user_model().objects.create_user(
            username="shelves",
            password="12345",  # noqa: S106
        )

    def test_custom_shelves_ratings_and_progress(self):
        """Tags become lists; exclusive shelves stay statuses."""
        rows = [
            _row(
                **{
                    "Book Id": "10",
                    "Title": "Read Book",
                    "Exclusive Shelf": "read",
                    "Bookshelves": " apple-books , read, kindle ",
                    "My Rating": "5",
                    "Average Rating": "4.29",
                    "Number of Pages": "120",
                    "Date Read": "2024/05/01",
                },
            ),
            _row(
                **{
                    "Book Id": "11",
                    "Title": "Now Book",
                    "Exclusive Shelf": "currently-reading",
                    "Bookshelves": "apple-books, currently-reading",
                    "Average Rating": "3.64",
                    "Number of Pages": "300",
                },
            ),
            _row(
                **{
                    "Book Id": "12",
                    "Title": "Plan Book",
                    "Exclusive Shelf": "to-read",
                    "Bookshelves": "to-read, to-buy",
                    "Average Rating": "4.00",
                    "Number of Pages": "80",
                },
            ),
            _row(
                **{
                    "Book Id": "13",
                    "Title": "Drop Book",
                    "Exclusive Shelf": "did-not-finish",
                    "Bookshelves": "did-not-finish, fidibo",
                    "Average Rating": "",
                    "Number of Pages": "40",
                },
            ),
        ]

        goodreads.importer(_csv_file(rows), self.user, "new")

        read = Book.objects.get(item__title="Read Book")
        now = Book.objects.get(item__title="Now Book")
        plan = Book.objects.get(item__title="Plan Book")
        dropped = Book.objects.get(item__title="Drop Book")
        self.assertEqual(read.status, Status.COMPLETED.value)
        self.assertEqual(read.progress, 120)
        self.assertEqual(read.score, 10)
        self.assertEqual(now.status, Status.IN_PROGRESS.value)
        self.assertEqual(now.progress, 0)
        self.assertEqual(plan.status, Status.PLANNING.value)
        self.assertEqual(dropped.status, Status.DROPPED.value)

        rating = read.item.public_rating
        self.assertEqual(rating.goodreads_id, "10")
        self.assertEqual(rating.goodreads_value, Decimal("4.29"))
        self.assertEqual(rating.goodreads_normalized, Decimal("85.8"))
        self.assertEqual(rating.page_count, 120)
        self.assertIsNone(rating.metacritic_value)
        self.assertIsNone(rating.metacritic_normalized)
        self.assertEqual(now.item.public_rating.page_count, 300)
        self.assertEqual(now.item.public_rating.goodreads_value, Decimal("3.64"))
        self.assertIsNone(dropped.item.public_rating.goodreads_value)
        self.assertEqual(dropped.item.public_rating.goodreads_id, "13")

        names = set(
            CustomList.objects.filter(owner=self.user).values_list("name", flat=True),
        )
        self.assertEqual(names, {"apple-books", "kindle", "to-buy", "fidibo"})
        apple = CustomList.objects.get(owner=self.user, name="apple-books")
        kindle = CustomList.objects.get(owner=self.user, name="kindle")
        self.assertEqual(
            set(apple.items.values_list("title", flat=True)),
            {"Read Book", "Now Book"},
        )
        self.assertEqual(
            set(kindle.items.values_list("title", flat=True)),
            {"Read Book"},
        )
        self.assertFalse(
            CustomList.objects.filter(
                name__in=["read", "currently-reading", "to-read", "did-not-finish"],
            ).exists(),
        )

    def test_missing_average_column_still_imports(self):
        """A newer export without Average Rating still imports the row."""
        row = _row(
            **{
                "Book Id": "77",
                "Title": "No Average",
                "Exclusive Shelf": "read",
                "Number of Pages": "50",
                "My Rating": "4",
            },
        )
        row.pop("Average Rating")
        fieldnames = [name for name in _row() if name != "Average Rating"]

        imported, warnings = goodreads.importer(
            _csv_file([row], fieldnames),
            self.user,
            "new",
        )

        book = Book.objects.get(user=self.user)
        self.assertEqual(imported, {MediaTypes.BOOK.value: 1})
        self.assertEqual(warnings, "")
        self.assertEqual(book.status, Status.COMPLETED.value)
        self.assertEqual(book.progress, 50)
        self.assertEqual(book.score, 8)
        rating = ItemRating.objects.get(item=book.item)
        self.assertEqual(rating.goodreads_id, "77")
        self.assertIsNone(rating.goodreads_value)
        self.assertIsNone(rating.goodreads_normalized)
        self.assertEqual(rating.page_count, 50)


PERSIAN_TITLE = "از نفس افتاده"
WRONG_TITLE = "شعله دوقلو : فراتر از آخرین نفس اثری از امیر قهرمان پور"


class ImportGoodreadsOffline(TestCase):
    """Each Goodreads row is its own item. No catalog lookup."""

    def setUp(self):
        """Create the user these imports belong to."""
        self.user = get_user_model().objects.create_user(
            username="offline",
            password="12345",  # noqa: S106
        )

    @patch("app.providers.openlibrary.book")
    @patch("app.providers.hardcover.book")
    @patch("integrations.imports.goodreads.services.search")
    def test_rows_become_goodreads_items(self, search, hardcover, library):
        """CSV title, author, year, and ISBN are stored. Catalogs are not called."""
        rows = [
            _row(
                Title=PERSIAN_TITLE,
                Author="امیر",
                ISBN13='="9786229101445"',
                Bookshelves="fidibo",
                **{
                    "Book Id": "237796490",
                    "Year Published": "1402",
                    "Original Publication Year": "1390",
                    "Number of Pages": "240",
                    "Average Rating": "4.29",
                    "Ratings Count": "1500",
                },
            ),
            _row(
                Title="Other Volume",
                ISBN13='=""',
                **{
                    "Book Id": "42",
                    "Year Published": "",
                    "Original Publication Year": "1999",
                },
            ),
        ]
        payload = _csv_file(rows)

        goodreads.importer(payload, self.user, "new")

        search.assert_not_called()
        hardcover.assert_not_called()
        library.assert_not_called()
        first = Item.objects.get(media_id="237796490")
        second = Item.objects.get(media_id="42")
        self.assertEqual(first.source, Sources.GOODREADS.value)
        self.assertEqual(first.title, PERSIAN_TITLE)
        self.assertEqual(first.image, settings.IMG_NONE)
        self.assertEqual(first.public_rating.author, "امیر")
        self.assertEqual(first.public_rating.publication_year, 1402)
        self.assertEqual(first.public_rating.isbn, "9786229101445")
        self.assertEqual(first.public_rating.goodreads_value, Decimal("4.29"))
        self.assertEqual(first.public_rating.goodreads_ratings_count, 1500)
        self.assertEqual(first.public_rating.page_count, 240)
        self.assertEqual(second.source, Sources.GOODREADS.value)
        self.assertEqual(second.media_id, "42")
        self.assertEqual(second.title, "Other Volume")
        self.assertEqual(second.public_rating.publication_year, 1999)
        self.assertEqual(second.public_rating.isbn, "")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Item.objects.count(), 2)
        self.assertEqual(Book.objects.filter(user=self.user).count(), 2)
        self.assertTrue(
            CustomList.objects.get(owner=self.user, name="fidibo")
            .items.filter(pk=first.pk)
            .exists(),
        )

        first.image = "https://example.com/kept.jpg"
        first.save(update_fields=["image"])
        payload.seek(0)
        goodreads.importer(payload, self.user, "new")

        first.refresh_from_db()
        self.assertEqual(Item.objects.filter(source=Sources.GOODREADS.value).count(), 2)
        self.assertEqual(Book.objects.filter(user=self.user).count(), 2)
        self.assertEqual(first.image, "https://example.com/kept.jpg")
        self.assertEqual(first.title, PERSIAN_TITLE)
        search.assert_not_called()
        hardcover.assert_not_called()
        library.assert_not_called()

    @patch("app.providers.openlibrary.book")
    @patch("app.providers.hardcover.book")
    @patch("integrations.imports.goodreads.services.search")
    def test_one_file_does_not_duplicate_a_book_id(self, search, hardcover, library):
        """Two copies of the same Book Id in one file share one item."""
        row = _row(Title=PERSIAN_TITLE, **{"Book Id": "7"})

        goodreads.importer(_csv_file([row, dict(row)]), self.user, "new")

        search.assert_not_called()
        hardcover.assert_not_called()
        library.assert_not_called()
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(Item.objects.get().media_id, "7")
        self.assertEqual(Book.objects.filter(user=self.user).count(), 1)
        self.assertFalse(Item.objects.filter(source=Sources.MANUAL.value).exists())


class GoodreadsCommands(TestCase):
    """Deploy commands for ratings, progress, and a mismatched import."""

    def setUp(self):
        """Create a user and a temporary directory for CSV files."""
        self.user = get_user_model().objects.create_user(
            username="reader",
            password="12345",  # noqa: S106
        )
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name, rows):
        """Write rows to a CSV and return its path."""
        path = Path(self.tmp.name) / name
        path.write_bytes(_csv_file(rows).getvalue())
        return path

    def _tracked(self, goodreads_id, average, pages, progress):
        """Create a book with a Goodreads rating."""
        item = Item.objects.create(
            media_id=goodreads_id,
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title=goodreads_id,
            image=settings.IMG_NONE,
        )
        ItemRating.objects.create(
            item=item,
            goodreads_id=goodreads_id,
            goodreads_value=average,
            page_count=pages,
        )
        book = Book.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Book.objects.filter(pk=book.pk).update(
            status=Status.IN_PROGRESS.value,
            progress=progress,
        )
        book.refresh_from_db()
        return book

    @patch("app.providers.services.get_media_metadata")
    def test_supplement_writes_ratings_and_progress(self, metadata):
        """Averages, vote counts, and pages read are stored without completing."""
        metadata.return_value = {"max_progress": 240}
        book = self._tracked("237796490", None, 0, 0)
        kept = self._tracked("77", Decimal("1.00"), 20, 1)
        other = get_user_model().objects.create_user(
            username="second",
            password="12345",  # noqa: S106
        )
        other_book = Book.objects.create(
            item=book.item,
            user=other,
            status=Status.PLANNING.value,
        )
        Book.objects.filter(pk=other_book.pk).update(
            status=Status.IN_PROGRESS.value,
            progress=0,
        )
        other_book.refresh_from_db()
        ratings = self._path(
            "ratings.csv",
            [
                {
                    "book_id": "237796490",
                    "average": "4.29",
                    "ratings_count": "1500",
                    "note": "ignore",
                },
                {
                    "book_id": "77",
                    "average": "",
                    "ratings_count": "9",
                    "note": "",
                },
                {
                    "book_id": "888",
                    "average": "3.50",
                    "ratings_count": "4",
                    "note": "",
                },
            ],
        )
        progress = self._path(
            "progress.csv",
            [
                {
                    "book_id": "237796490",
                    "pages_read": "240",
                    "total_pages": "240",
                    "percent": "100",
                },
                {
                    "book_id": "missing",
                    "pages_read": "4",
                    "total_pages": "10",
                    "percent": "40",
                },
            ],
        )
        output = StringIO()

        call_command(
            "apply_goodreads_supplement",
            str(ratings),
            "--progress",
            str(progress),
            "--username",
            self.user.username,
            stdout=output,
        )

        book.refresh_from_db()
        kept.refresh_from_db()
        other_book.refresh_from_db()
        rating = book.item.public_rating
        rating.refresh_from_db()
        metadata.assert_not_called()
        self.assertEqual(rating.goodreads_value, Decimal("4.29"))
        self.assertEqual(rating.goodreads_normalized, Decimal("85.8"))
        self.assertEqual(rating.goodreads_ratings_count, 1500)
        self.assertEqual(rating.page_count, 240)
        self.assertEqual(book.progress, 240)
        self.assertEqual(book.status, Status.IN_PROGRESS.value)
        self.assertEqual(kept.item.public_rating.goodreads_value, Decimal("1.00"))
        self.assertIsNone(kept.item.public_rating.goodreads_ratings_count)
        self.assertEqual(other_book.progress, 0)
        self.assertIn("Updated 2.", output.getvalue())
        self.assertIn("Missing 2.", output.getvalue())
        self.assertIn("Repaired 0.", output.getvalue())
        self.assertIn("Created 0.", output.getvalue())

    @patch("app.providers.openlibrary.book")
    @patch("app.providers.hardcover.book")
    @patch("integrations.imports.goodreads.services.search")
    def test_repair_command_imports_and_repairs(self, search, hardcover, library):
        """The command imports a new row and moves a book onto its Goodreads item."""
        wrong = Item.objects.create(
            media_id="bad-hc",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title=WRONG_TITLE,
            image="http://example.com/wrong.jpg",
        )
        ItemRating.objects.create(item=wrong, goodreads_id="237796490")
        book = Book.objects.create(
            item=wrong,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Book.objects.filter(pk=book.pk).update(status=Status.IN_PROGRESS.value)
        path = self._path(
            "export.csv",
            [
                _row(
                    Title=PERSIAN_TITLE,
                    ISBN13='="9786229101445"',
                    **{"Book Id": "237796490"},
                ),
                _row(
                    Title="A Missing Book",
                    ISBN13='=""',
                    **{"Book Id": "42", "Exclusive Shelf": "to-read"},
                ),
            ],
        )
        output = StringIO()

        call_command(
            "repair_goodreads_import",
            str(path),
            "--username",
            self.user.username,
            stdout=output,
        )

        book.refresh_from_db()
        wrong.refresh_from_db()
        search.assert_not_called()
        hardcover.assert_not_called()
        library.assert_not_called()
        self.assertEqual(wrong.title, WRONG_TITLE)
        self.assertEqual(wrong.image, "http://example.com/wrong.jpg")
        self.assertEqual(wrong.public_rating.goodreads_id, "")
        self.assertEqual(book.item.title, PERSIAN_TITLE)
        self.assertEqual(book.item.source, Sources.GOODREADS.value)
        self.assertEqual(book.item.media_id, "237796490")
        self.assertEqual(book.status, Status.IN_PROGRESS.value)
        self.assertTrue(
            Item.objects.filter(
                source=Sources.GOODREADS.value,
                media_id="42",
                title="A Missing Book",
                public_rating__goodreads_id="42",
            ).exists(),
        )
        self.assertFalse(Item.objects.filter(source=Sources.MANUAL.value).exists())
        self.assertEqual(Book.objects.filter(user=self.user).count(), 2)
        summary = output.getvalue()
        self.assertIn("Repaired 1.", summary)
        self.assertIn("Created 1.", summary)
        self.assertIn("Missing 0.", summary)

    def test_supplement_writes_cover_urls(self):
        """A cover URL is stored on the Goodreads item and does not clear a blank."""
        item = Item.objects.create(
            media_id="237796490",
            source=Sources.GOODREADS.value,
            media_type=MediaTypes.BOOK.value,
            title=PERSIAN_TITLE,
            image=settings.IMG_NONE,
        )
        ItemRating.objects.create(item=item, goodreads_id="237796490")
        kept = Item.objects.create(
            media_id="77",
            source=Sources.GOODREADS.value,
            media_type=MediaTypes.BOOK.value,
            title="Kept",
            image="https://example.com/kept.jpg",
        )
        ItemRating.objects.create(item=kept, goodreads_id="77")
        ratings = self._path(
            "ratings.csv",
            [
                {
                    "book_id": "237796490",
                    "average": "4.00",
                    "ratings_count": "3",
                },
            ],
        )
        covers = self._path(
            "covers.csv",
            [
                {
                    "book_id": "237796490",
                    "cover_url": "https://example.com/new.jpg",
                },
                {"book_id": "77", "cover_url": ""},
                {
                    "book_id": "missing",
                    "cover_url": "https://example.com/nope.jpg",
                },
            ],
        )
        output = StringIO()

        call_command(
            "apply_goodreads_supplement",
            str(ratings),
            "--covers",
            str(covers),
            "--username",
            self.user.username,
            stdout=output,
        )

        item.refresh_from_db()
        kept.refresh_from_db()
        rating = item.public_rating
        rating.refresh_from_db()
        self.assertEqual(item.image, "https://example.com/new.jpg")
        self.assertEqual(kept.image, "https://example.com/kept.jpg")
        self.assertEqual(rating.goodreads_value, Decimal("4.00"))
        self.assertEqual(rating.goodreads_ratings_count, 3)
        self.assertIn("Updated 2.", output.getvalue())
        self.assertIn("Missing 1.", output.getvalue())


class GoodreadsMetadata(TestCase):
    """Detail pages read the database and do not call a book catalog."""

    def setUp(self):
        """Create a logged-in user and one Goodreads book."""
        self.credentials = {"username": "meta", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.item = Item.objects.create(
            media_id="237796490",
            source=Sources.GOODREADS.value,
            media_type=MediaTypes.BOOK.value,
            title=PERSIAN_TITLE,
            image="https://example.com/cover.jpg",
        )
        ItemRating.objects.create(
            item=self.item,
            goodreads_id="237796490",
            goodreads_value=Decimal("4.29"),
            goodreads_normalized=Decimal("85.8"),
            goodreads_ratings_count=1500,
            page_count=240,
            author="امیر",
            publication_year=1402,
            isbn="9786229101445",
        )

    @patch("app.providers.services.api_request")
    @patch("app.providers.openlibrary.book")
    @patch("app.providers.hardcover.book")
    def test_metadata_and_detail_page_use_the_database(self, hardcover, library, api):
        """Opening a stored Goodreads book does not call Open Library or Hardcover."""
        metadata = services.get_media_metadata(
            MediaTypes.BOOK.value,
            "237796490",
            Sources.GOODREADS.value,
        )

        self.assertEqual(metadata["title"], PERSIAN_TITLE)
        self.assertEqual(metadata["image"], "https://example.com/cover.jpg")
        self.assertEqual(metadata["max_progress"], 240)
        self.assertEqual(metadata["score"], 8.6)
        self.assertEqual(metadata["score_count"], 1500)
        self.assertEqual(metadata["details"]["author"], "امیر")
        self.assertEqual(metadata["details"]["number_of_pages"], 240)
        self.assertEqual(metadata["details"]["year"], 1402)
        self.assertEqual(metadata["details"]["isbn"], "9786229101445")
        self.assertEqual(
            metadata["source_url"],
            "https://www.goodreads.com/book/show/237796490",
        )
        hardcover.assert_not_called()
        library.assert_not_called()
        api.assert_not_called()

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.GOODREADS.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "237796490",
                    "title": "book",
                },
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media"]["title"], PERSIAN_TITLE)
        self.assertContains(response, "https://www.goodreads.com/book/show/237796490")
        hardcover.assert_not_called()
        library.assert_not_called()
        api.assert_not_called()

    def test_missing_item_raises_not_found(self):
        """A Goodreads id with no item is the usual not-found error."""
        with self.assertRaises(services.ProviderAPIError) as caught:
            services.get_media_metadata(
                MediaTypes.BOOK.value,
                "missing",
                Sources.GOODREADS.value,
            )
        self.assertEqual(caught.exception.status_code, 404)

    @patch("app.providers.services.api_request")
    @patch("app.providers.openlibrary.book")
    @patch("app.providers.hardcover.book")
    def test_sync_does_not_call_a_provider(self, hardcover, library, api):
        """Sync refuses a Goodreads item instead of contacting a catalog."""
        response = self.client.post(
            reverse(
                "sync_metadata",
                kwargs={
                    "source": Sources.GOODREADS.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "237796490",
                },
            ),
        )
        self.assertEqual(response.status_code, 400)
        hardcover.assert_not_called()
        library.assert_not_called()
        api.assert_not_called()


class CleanupOrphanBookItems(TestCase):
    """The cleanup command deletes only untracked book items."""

    def setUp(self):
        """Create the user who owns the list on the orphan book."""
        self.user = get_user_model().objects.create_user(
            username="cleaner",
            password="12345",  # noqa: S106
        )

    def _item(self, media_id, media_type, source):
        """Create one catalog item."""
        return Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            title=media_id,
            image=settings.IMG_NONE,
        )

    def test_deletes_untracked_books_only(self):
        """A book with no Book row goes. Tracked books and other media stay."""
        orphan = self._item("orphan", MediaTypes.BOOK.value, Sources.GOODREADS.value)
        ItemRating.objects.create(item=orphan, goodreads_id="orphan", page_count=10)
        shelf = CustomList.objects.create(owner=self.user, name="fidibo")
        shelf.items.add(orphan)
        Event.objects.create(item=orphan, datetime=timezone.now())
        kept = self._item("kept", MediaTypes.BOOK.value, Sources.HARDCOVER.value)
        ItemRating.objects.create(item=kept, goodreads_id="kept")
        Book.objects.create(
            item=kept,
            user=self.user,
            status=Status.PLANNING.value,
        )
        movie = self._item("movie", MediaTypes.MOVIE.value, Sources.TMDB.value)
        tv = self._item("tv", MediaTypes.TV.value, Sources.TMDB.value)
        game = self._item("game", MediaTypes.GAME.value, Sources.IGDB.value)
        output = StringIO()

        call_command("cleanup_orphan_book_items", "--dry-run", stdout=output)

        self.assertTrue(Item.objects.filter(pk=orphan.pk).exists())
        self.assertIn(
            "Would delete 1 book items, 1 ratings, 1 list items, 1 events.",
            output.getvalue(),
        )

        output = StringIO()
        call_command("cleanup_orphan_book_items", stdout=output)

        self.assertFalse(Item.objects.filter(pk=orphan.pk).exists())
        self.assertFalse(ItemRating.objects.filter(goodreads_id="orphan").exists())
        self.assertFalse(CustomListItem.objects.filter(item_id=orphan.id).exists())
        self.assertFalse(Event.objects.filter(item_id=orphan.id).exists())
        self.assertTrue(Item.objects.filter(pk=kept.pk).exists())
        self.assertTrue(ItemRating.objects.filter(item=kept).exists())
        self.assertTrue(Book.objects.filter(item=kept).exists())
        self.assertTrue(Item.objects.filter(pk=movie.pk).exists())
        self.assertTrue(Item.objects.filter(pk=tv.pk).exists())
        self.assertTrue(Item.objects.filter(pk=game.pk).exists())
        self.assertIn(
            "Deleted 1 book items, 1 ratings, 1 list items, 1 events.",
            output.getvalue(),
        )
