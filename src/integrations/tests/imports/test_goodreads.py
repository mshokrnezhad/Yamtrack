from csv import DictReader, DictWriter
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
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
from integrations.imports import (
    goodreads,
)
from lists.models import CustomList

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


class ImportGoodreadsMultilineNotes(TestCase):
    """Test importing GoodReads rows whose quoted fields span several lines."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    @patch("integrations.imports.goodreads.GoodReadsImporter._search_book")
    def test_multiline_notes_are_preserved(self, mock_search_book):
        """Test that quoted multiline notes keep embedded newlines."""
        mock_search_book.return_value = {
            "media_id": "1215032",
            "title": "The Wise Man's Fear",
            "image": "https://example.com/image.jpg",
        }

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
        with patch.object(self.importer, "_search_book") as mock_search:
            self.importer._process_row(row)
        mock_search.assert_not_called()
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
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="The Wise Man's Fear",
            image="https://example.com/image.jpg",
        )
        Book.objects.bulk_create(
            [Book(item=item, user=self.user, status=Status.PLANNING.value)],
        )

    @patch("integrations.imports.goodreads.GoodReadsImporter._search_book")
    def run_import(self, mock_search_book, **fields):
        """Run a GoodReads import in "new" mode of the existing book."""
        mock_search_book.return_value = {
            "media_id": "1215032",
            "title": "The Wise Man's Fear",
            "image": "https://example.com/image.jpg",
        }

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

    @patch("integrations.imports.goodreads.services.search")
    def test_empty_isbn_searches_by_title_only(self, search):
        """An empty formula is missing, so the title is the only query."""
        self.user = get_user_model().objects.create_user(
            username="isbn",
            password="12345",  # noqa: S106
        )
        importer = goodreads.GoodReadsImporter(None, self.user, "new")
        search.return_value = {"results": []}

        with Path(mock_path / "import_goodreads.csv").open(newline="") as file:
            reader = DictReader(file)
            empty = next(row for row in reader if row["Book Id"] == "28825810")
            wrapped = next(row for row in reader if row["Book Id"] == "1215032")

        importer._search_book(empty, Sources.HARDCOVER)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.args[1], empty["Title"])

        search.reset_mock()
        search.return_value = {"results": []}
        importer._search_book(wrapped, Sources.HARDCOVER)
        self.assertEqual(search.call_args_list[0].args[1], "9780756404734")
        self.assertEqual(search.call_args_list[1].args[1], wrapped["Title"])

    @patch("integrations.imports.goodreads.services.search")
    def test_wrapped_isbn_is_the_search_query(self, search):
        """A hit on the unwrapped ISBN does not also search the formula."""
        self.user = get_user_model().objects.create_user(
            username="wrap",
            password="12345",  # noqa: S106
        )
        search.return_value = {
            "results": [
                {
                    "media_id": "hc-1",
                    "title": "Wrapped",
                    "image": "http://example.com/a.jpg",
                },
            ],
        }
        row = _row(
            Title="Wrapped",
            ISBN13='="9789644480645"',
            **{"Exclusive Shelf": "to-read", "Book Id": "99"},
        )

        goodreads.importer(_csv_file([row]), self.user, "new")

        self.assertEqual(search.call_count, 1)
        self.assertEqual(
            search.call_args.args,
            (MediaTypes.BOOK.value, "9789644480645", 1, Sources.HARDCOVER.value),
        )


class ImportGoodreadsMetadata(TestCase):
    """Goodreads id, community average, page count, and custom shelves."""

    def setUp(self):
        """Create a user for the import."""
        self.user = get_user_model().objects.create_user(
            username="shelves",
            password="12345",  # noqa: S106
        )

    def _search(self, _media_type, query, _page, _source):
        return {
            "results": [
                {
                    "media_id": query[:36],
                    "title": query,
                    "image": "http://example.com/a.jpg",
                },
            ],
        }

    @patch("integrations.imports.goodreads.services.search")
    def test_custom_shelves_ratings_and_progress(self, search):
        """Tags become lists; exclusive shelves stay statuses."""
        search.side_effect = self._search
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

    @patch("integrations.imports.goodreads.services.search")
    def test_missing_average_column_still_imports(self, search):
        """A newer export without Average Rating still imports the row."""
        search.side_effect = self._search
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
