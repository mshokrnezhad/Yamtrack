from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import Book, Item, ItemRating, MediaTypes, Sources, Status
from lists.models import CustomList

METADATA = {
    "title": "Test",
    "image": "http://example.com/a.jpg",
    "max_progress": None,
}
EXCLUSIVE = ("read", "currently-reading", "to-read", "did-not-finish")


class DashboardBookTests(TestCase):
    """Books dashboard rows, separate from movies and games."""

    def setUp(self):
        """Log in and keep book saves off the network."""
        self.user = get_user_model().objects.create_user(
            username="reader",
            password="test-pass",  # noqa: S106
        )
        self.client.login(username="reader", password="test-pass")  # noqa: S106
        self.metadata = patch(
            "app.providers.services.get_media_metadata",
            return_value=METADATA,
        )
        self.releases = patch("app.models.Item.fetch_releases")
        self.metadata.start()
        self.releases.start()
        self.addCleanup(self.metadata.stop)
        self.addCleanup(self.releases.stop)

    def test_currently_reading_orders_by_progress_and_shows_pages(self):
        """In-progress books show pages read, and a high rating does not sort first."""
        older = timezone.now() - timedelta(days=3)
        zulu = self._book("zulu", "Zulu", Status.IN_PROGRESS.value, progress=0)
        alpha = self._book("alpha", "Alpha", Status.IN_PROGRESS.value, progress=40)
        bravo = self._book("bravo", "Bravo", Status.IN_PROGRESS.value, progress=0)
        self._rating(zulu.item, Decimal("1.20"), 320, "1")
        self._rating(alpha.item, Decimal("4.90"), 200, "2")
        self._rating(bravo.item, Decimal("4.80"), 200, "3")
        Book.objects.filter(pk=alpha.pk).update(progressed_at=older)
        Book.objects.filter(pk=bravo.pk).update(progressed_at=older)
        Book.history.model.objects.filter(id__in=[alpha.pk, bravo.pk]).update(
            history_date=older,
        )

        response = self.client.get(reverse("dashboard_books"))
        row = self._row(response, "Currently reading")
        page = self.client.get(reverse("dashboard_row", args=["currently-reading"]))
        titles = [entry.item.title for entry in row["media"]]

        self.assertEqual(titles, ["Zulu", "Alpha", "Bravo"])
        self.assertEqual(
            [entry.item.title for entry in page.context["entries"]],
            titles,
        )
        self.assertEqual(page.context["back_url"], reverse("dashboard_books"))
        self.assertEqual(row["media"][0].progress, 0)
        self.assertEqual(row["media"][0].max_progress, 320)
        self.assertEqual(row["media"][1].progress, 40)
        self.assertEqual(row["media"][1].max_progress, 200)
        self.assertContains(response, "/ 320")
        self.assertContains(response, "/ 200")
        self.assertContains(response, "width: 0%")
        self.assertContains(response, "width: 20%")
        self.assertNotContains(response, "Refresh")

    def test_tag_rows_skip_completed_and_sort_by_average(self):
        """Unread books on a tag sort by Goodreads average; finished books stay off."""
        paperback = self._list("paperback")
        kindle = self._list("kindle")
        exclusive = {name: self._list(name) for name in EXCLUSIVE}
        xbox = self._list("Xbox")
        switch = self._list("Nintendo Switch")
        xbox.items.add(
            self._item("halo", "Halo", MediaTypes.GAME.value, Sources.IGDB.value),
        )
        switch.items.add(
            self._item("mario", "Mario", MediaTypes.GAME.value, Sources.IGDB.value),
        )

        specs = (
            ("bravo", "Bravo", Decimal("4.55"), Status.IN_PROGRESS.value, True),
            ("alpha", "Alpha", Decimal("4.29"), Status.PLANNING.value, True),
            ("amy", "Amy", Decimal("3.50"), Status.PLANNING.value, False),
            ("zed", "Zed", Decimal("3.50"), Status.PAUSED.value, False),
            ("mid", "Mid", Decimal("3.00"), Status.PAUSED.value, False),
            ("drop", "Drop", Decimal("2.00"), Status.DROPPED.value, False),
            ("loose", "Loose", Decimal("1.10"), None, False),
            ("aaron", "Aaron", None, Status.PLANNING.value, False),
            ("zora", "Zora", None, Status.PLANNING.value, False),
            ("done", "Done", Decimal("4.99"), Status.COMPLETED.value, True),
        )
        for media_id, title, average, status, on_kindle in specs:
            item = self._item(media_id, title)
            paperback.items.add(item)
            if on_kindle:
                kindle.items.add(item)
            if average is not None or media_id == "aaron":
                self._rating(item, average, 100, media_id)
            if status is not None:
                self._book(media_id, title, status, item=item)
            for custom_list in exclusive.values():
                custom_list.items.add(item)

        response = self.client.get(reverse("dashboard_books"))
        titles = [str(row["title"]) for row in response.context["rows"]]
        movies = self.client.get(reverse("dashboard_movies"))
        games = self.client.get(reverse("dashboard_games"))
        series = self.client.get(reverse("dashboard_series"))

        self.assertEqual(titles, ["Currently reading", "kindle", "paperback"])
        self.assertEqual(self._titles(response, "Currently reading"), ["Bravo"])
        self.assertEqual(self._titles(response, "kindle"), ["Bravo", "Alpha"])
        self.assertEqual(
            self._titles(response, "paperback"),
            ["Bravo", "Alpha", "Amy", "Zed", "Mid", "Drop", "Loose", "Aaron", "Zora"],
        )
        alpha = self._row(response, "paperback")["media"][1]
        self.assertEqual(alpha.public_rating, Decimal("4.29"))
        self.assertContains(response, ">4.29<")
        self.assertContains(response, ">4.55<")
        self.assertNotContains(response, "Done")
        self.assertNotContains(response, "Halo")
        self.assertNotContains(response, "Mario")
        self.assertNotContains(response, "Refresh")
        # The sidebar names every row on every page, so compare the rows.
        for page in (movies, series, games):
            page_titles = [str(row["title"]) for row in page.context["rows"]]
            self.assertNotIn("Currently reading", page_titles)
            self.assertNotIn("paperback", page_titles)
        for name in (*EXCLUSIVE, "Xbox", "Nintendo Switch"):
            self.assertNotIn(name, titles)

        kindle_page = self.client.get(self._row(response, "kindle")["see_all"])
        self.assertEqual(kindle_page.context["back_url"], reverse("dashboard_books"))
        self.assertEqual(
            [entry.item.title for entry in kindle_page.context["entries"]],
            ["Bravo", "Alpha"],
        )

    def test_see_all_matches_the_tag_row_past_the_cap(self):
        """The row stops at ten and See all keeps that order for every unread book."""
        shelf = self._list("audiobook")
        total = 11
        for number in range(total):
            item = self._item(f"unread-{number}", f"Unread {number:02d}")
            score = Decimal("5.00") - (Decimal(number) / Decimal(10))
            self._rating(item, score, 10, str(number))
            self._book(
                f"unread-{number}",
                f"Unread {number:02d}",
                Status.PLANNING.value,
                item=item,
            )
            shelf.items.add(item)
        finished = self._item("finished", "Finished")
        self._rating(finished, Decimal("5.00"), 10, "done")
        self._book("finished", "Finished", Status.COMPLETED.value, item=finished)
        shelf.items.add(finished)

        dashboard = self.client.get(reverse("dashboard_books"))
        row = self._row(dashboard, "audiobook")
        page = self.client.get(row["see_all"])
        shown = [entry.item.title for entry in page.context["entries"]]
        expected = [entry.item.title for entry in row["media"]]

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["back_url"], reverse("dashboard_books"))
        self.assertEqual(len(row["media"]), 10)
        self.assertEqual(shown[:10], expected)
        self.assertEqual(shown, [f"Unread {number:02d}" for number in range(total)])
        self.assertNotIn("Finished", shown)
        self.assertNotContains(dashboard, "Unread 10")
        self.assertContains(page, "Unread 10")

    def test_empty_books_page(self):
        """No in-progress books and no tag lists is an empty page, not an error."""
        response = self.client.get(reverse("dashboard_books"))
        page = self.client.get(reverse("dashboard_row", args=["currently-reading"]))
        missing = self.client.get(reverse("dashboard_row", args=["book-tag-999999"]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/dashboard.html")
        self.assertEqual(
            [str(row["title"]) for row in response.context["rows"]],
            ["Currently reading"],
        )
        self.assertEqual(response.context["rows"][0]["media"], [])
        self.assertFalse(response.context["show_refresh"])
        self.assertContains(response, "No books in progress.")
        self.assertNotContains(response, "Refresh")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["back_url"], reverse("dashboard_books"))
        self.assertContains(page, "No books in progress.")
        self.assertEqual(missing.status_code, 404)

    def _row(self, response, title):
        return next(
            row for row in response.context["rows"] if str(row["title"]) == title
        )

    def _titles(self, response, title):
        return [entry.item.title for entry in self._row(response, title)["media"]]

    def _list(self, name):
        return CustomList.objects.create(name=name, owner=self.user)

    def _item(self, media_id, title, media_type=None, source=None):
        return Item.objects.create(
            media_id=media_id,
            source=source or Sources.HARDCOVER.value,
            media_type=media_type or MediaTypes.BOOK.value,
            title=title,
            image="http://example.com/a.jpg",
        )

    def _book(self, media_id, title, status, progress=0, item=None):
        return Book.objects.create(
            item=item or self._item(media_id, title),
            user=self.user,
            status=status,
            progress=progress,
        )

    def _rating(self, item, average, pages, goodreads_id):
        return ItemRating.objects.create(
            item=item,
            goodreads_id=goodreads_id,
            goodreads_value=average,
            page_count=pages,
        )
