from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    TV,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)


class SearchParentViewTests(TestCase):
    """Test the parent search views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        tv_item1 = Item.objects.create(
            media_id="111",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )
        self.tv1 = TV.objects.create(
            item=tv_item1,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        tv_item2 = Item.objects.create(
            media_id="222",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Another TV Show",
        )
        self.tv2 = TV.objects.create(
            item=tv_item2,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item1 = Item.objects.create(
            media_id="111",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Test Season",
            season_number=1,
        )
        self.season1 = Season.objects.create(
            item=season_item1,
            user=self.user,
            related_tv=self.tv1,
            status=Status.IN_PROGRESS.value,
        )

        season_item2 = Item.objects.create(
            media_id="222",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Another Season",
            season_number=1,
        )
        self.season2 = Season.objects.create(
            item=season_item2,
            user=self.user,
            related_tv=self.tv2,
            status=Status.IN_PROGRESS.value,
        )

    def test_search_parent_tv_short_query(self):
        """Test search_parent_tv with a query that's too short."""
        response = self.client.get(reverse("search_parent_tv") + "?q=T")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertNotIn("results", response.context)

    def test_search_parent_tv_valid_query(self):
        """Test search_parent_tv with a valid query."""
        response = self.client.get(reverse("search_parent_tv") + "?q=Test")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertIn("results", response.context)
        self.assertIn("query", response.context)

        self.assertEqual(len(response.context["results"]), 1)
        self.assertEqual(response.context["results"][0], self.tv1)
        self.assertEqual(response.context["query"], "Test")

    def test_search_parent_tv_no_results(self):
        """Test search_parent_tv with a query that returns no results."""
        response = self.client.get(reverse("search_parent_tv") + "?q=NonExistent")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertIn("results", response.context)

        self.assertEqual(len(response.context["results"]), 0)

    def test_search_parent_season_short_query(self):
        """Test search_parent_season with a query that's too short."""
        response = self.client.get(reverse("search_parent_season") + "?q=T")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertNotIn("results", response.context)

    def test_search_parent_season_valid_query(self):
        """Test search_parent_season with a valid query."""
        response = self.client.get(reverse("search_parent_season") + "?q=Test")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_season.html")
        self.assertIn("results", response.context)
        self.assertIn("query", response.context)

        self.assertEqual(len(response.context["results"]), 1)
        self.assertEqual(response.context["results"][0], self.season1)
        self.assertEqual(response.context["query"], "Test")

    def test_search_parent_season_no_results(self):
        """Test search_parent_season with a query that returns no results."""
        response = self.client.get(reverse("search_parent_season") + "?q=NonExistent")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_season.html")
        self.assertIn("results", response.context)

        self.assertEqual(len(response.context["results"]), 0)

    def test_empty_query_lists_every_custom_show_and_season(self):
        """Focusing an empty field lists the user's shows and seasons by title."""
        shows = self.client.get(reverse("search_parent_tv") + "?q=")
        seasons = self.client.get(reverse("search_parent_season") + "?q=")

        self.assertEqual(list(shows.context["results"]), [self.tv2, self.tv1])
        self.assertEqual(
            list(seasons.context["results"]),
            [self.season2, self.season1],
        )
        self.assertNotContains(shows, "No TV shows found")
        self.assertNotContains(seasons, "No seasons found")

    def test_season_number_query_matches_the_season(self):
        """Typing a season number finds that season of any custom show."""
        response = self.client.get(reverse("search_parent_season") + "?q=1")

        self.assertEqual(
            list(response.context["results"]),
            [self.season2, self.season1],
        )
        none = self.client.get(reverse("search_parent_season") + "?q=7")
        self.assertEqual(list(none.context["results"]), [])

        self.season1.item.season_number = 2
        self.season1.item.title = "Show (1964)"
        self.season1.item.save()
        second = self.client.get(reverse("search_parent_season") + "?q=2")
        self.assertEqual(list(second.context["results"]), [self.season1])
        by_title = self.client.get(reverse("search_parent_season") + "?q=1964")
        self.assertEqual(list(by_title.context["results"]), [self.season1])

    def test_show_title_with_quote_is_escaped_for_the_picker(self):
        """A quote in a show title cannot break the click handler."""
        self.tv1.item.title = "Bob's Show"
        self.tv1.item.save()

        response = self.client.get(reverse("search_parent_tv") + "?q=Bob")

        backslash = chr(92)
        self.assertContains(response, f"Bob{backslash}u0027s Show")
        self.assertNotContains(response, "'Bob's Show'")
