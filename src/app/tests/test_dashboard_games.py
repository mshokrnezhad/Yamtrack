from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import Game, Item, ItemRating, MediaTypes, Sources, Status
from app.providers import igdb
from app.providers.services import ProviderAPIError
from app.ratings import (
    RATED_MEDIA_TYPES,
    refresh_game_critic_scores,
    refresh_stale_ratings,
)
from lists.models import CustomList

GAME_TITLES = (
    "Continue Playing - Xbox",
    "Continue Playing - Switch",
    "Best Owned - Xbox",
    "Best Owned - Switch",
)
MOVIE_ROW_KEYS = (
    "top-downloaded-movies",
    "top-planning-movies",
    "top-downloaded-tv",
    "top-planning-tv",
    "continue-watching",
    "new-episodes",
)
METADATA = {
    "title": "Test",
    "image": "http://example.com/a.jpg",
    "max_progress": None,
}


class DashboardChooserTests(TestCase):
    """The landing page and the split between the dashboards."""

    def setUp(self):
        """Log in a user."""
        get_user_model().objects.create_user(
            username="chooser",
            password="test-pass",  # noqa: S106
        )
        self.client.login(username="chooser", password="test-pass")  # noqa: S106

    def test_chooser_links_to_each_dashboard(self):
        """GET /dashboard is the tile page, and / still redirects there."""
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/dashboard_home.html")
        self.assertContains(response, f'href="{reverse("dashboard_movies_tv")}"')
        self.assertContains(response, f'href="{reverse("dashboard_games")}"')
        self.assertContains(response, f'href="{reverse("dashboard_books")}"')
        self.assertContains(response, "Movies &amp; TV")
        self.assertNotContains(response, "Movies & TV")
        self.assertContains(response, "Games")
        self.assertContains(response, "Books")

        root = self.client.get("/")

        self.assertEqual(root.status_code, 302)
        self.assertEqual(root.url, "/dashboard")

    def test_movie_and_game_pages_do_not_share_rows(self):
        """Each dashboard renders only its own rows."""
        movies = self.client.get(reverse("dashboard_movies_tv"))
        games = self.client.get(reverse("dashboard_games"))
        content = games.content.decode()

        self.assertContains(movies, "Continue watching")
        self.assertContains(movies, "New episodes")
        self.assertContains(movies, "Refresh")
        self.assertContains(movies, "Movies &amp; TV")
        self.assertNotContains(movies, "Continue Playing - Xbox")
        self.assertNotContains(movies, "Best Owned - Switch")
        self.assertNotContains(games, "Continue watching")
        self.assertNotContains(games, "New episodes")
        self.assertNotContains(games, "Top Downloaded movies")
        self.assertNotContains(games, "Top Planning movies")
        self.assertNotContains(games, "Top Downloaded TV")
        self.assertNotContains(games, "Top Planning TV")
        self.assertNotContains(games, "Refresh")
        positions = []
        for title in GAME_TITLES:
            self.assertContains(games, title)
            positions.append(content.index(title))
        self.assertEqual(positions, sorted(positions))
        self.assertContains(games, "No games in progress.")
        self.assertContains(games, "No unfinished owned games.")

    def test_see_all_urls_still_resolve(self):
        """Movie rows and the four game rows each return a page."""
        for key in MOVIE_ROW_KEYS:
            page = self.client.get(reverse("dashboard_row", args=[key]))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, f'href="{reverse("dashboard_movies_tv")}"')
        for key in (
            "continue-playing-xbox",
            "continue-playing-switch",
            "best-owned-xbox",
            "best-owned-switch",
        ):
            page = self.client.get(reverse("dashboard_row", args=[key]))
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, f'href="{reverse("dashboard_games")}"')
            empty = "No games in progress." if "continue" in key else "No unfinished"
            self.assertContains(page, empty)


class DashboardGameRowTests(TestCase):
    """Selection and ordering for the four game rows."""

    def setUp(self):
        """Log in and keep game saves off the network."""
        self.user = get_user_model().objects.create_user(
            username="player",
            password="test-pass",  # noqa: S106
        )
        self.client.login(username="player", password="test-pass")  # noqa: S106
        self.metadata = patch(
            "app.providers.services.get_media_metadata",
            return_value=METADATA,
        )
        self.releases = patch("app.models.Item.fetch_releases")
        self.metadata.start()
        self.releases.start()
        self.addCleanup(self.metadata.stop)
        self.addCleanup(self.releases.stop)

    def test_continue_playing_uses_both_lists_and_active_statuses(self):
        """In progress and paused games on a list are shown, on each platform."""
        xbox = self._list("Xbox")
        switch = self._list("Nintendo Switch")
        halo = self._item("halo", "Halo")
        mario = self._item("mario", "Mario")
        figment = self._item("figment", "Figment")
        figment_2 = self._item("figment-2", "Figment 2")
        later = self._item("later", "Later")
        done = self._item("done", "Done")
        quit_game = self._item("quit", "Quit")
        elsewhere = self._item("elsewhere", "Elsewhere")
        xbox.items.add(halo, figment, figment_2, later, done, quit_game)
        switch.items.add(mario, figment, figment_2)
        self._track(halo, Status.IN_PROGRESS.value)
        self._track(mario, Status.PAUSED.value)
        self._track(figment, Status.IN_PROGRESS.value)
        self._track(figment_2, Status.PAUSED.value)
        self._track(later, Status.PLANNING.value)
        self._track(done, Status.COMPLETED.value)
        self._track(quit_game, Status.DROPPED.value)
        self._track(elsewhere, Status.IN_PROGRESS.value)

        self.assertEqual(
            set(self._titles("Continue Playing - Xbox")),
            {"Halo", "Figment", "Figment 2"},
        )
        self.assertEqual(
            set(self._titles("Continue Playing - Switch")),
            {"Mario", "Figment", "Figment 2"},
        )

    def test_continue_playing_orders_by_latest_history_then_title(self):
        """Saving a game makes it the most recently active title."""
        xbox = self._list("Xbox")
        alpha = self._track(self._item("alpha", "Alpha"), Status.IN_PROGRESS.value)
        beta = self._track(self._item("beta", "Beta"), Status.IN_PROGRESS.value)
        xbox.items.add(alpha.item, beta.item)
        Game.history.model.objects.filter(id__in=[alpha.pk, beta.pk]).update(
            history_date=timezone.now() - timedelta(days=3),
        )

        self.assertEqual(self._titles("Continue Playing - Xbox"), ["Alpha", "Beta"])

        beta.notes = "picked back up"
        beta.save()

        self.assertEqual(self._titles("Continue Playing - Xbox"), ["Beta", "Alpha"])

    def test_best_owned_orders_by_critic_score_and_shows_the_rounded_badge(self):
        """Unfinished and untracked games sort by the exact score, nulls last."""
        xbox = self._list("Xbox")
        specs = (
            ("bravo", "Bravo", Decimal("91.4"), None),
            ("alpha", "Alpha", Decimal("91.2"), None),
            ("also", "Also", Decimal(50), None),
            ("mid", "Mid", Decimal(50), None),
            ("soon", "Soon", Decimal(40), Status.PLANNING.value),
            ("fresh", "Fresh", Decimal(10), None),
            ("amy", "Amy", None, None),
            ("zed", "Zed", None, None),
            ("done", "Done", Decimal(99), Status.COMPLETED.value),
            ("quit", "Quit", Decimal(95), Status.DROPPED.value),
        )
        for media_id, title, score, status in specs:
            item = self._item(media_id, title)
            xbox.items.add(item)
            if score is not None:
                self._critic(item, score)
            if status is not None:
                self._track(item, status)
        outsider = self._item("outsider", "Outsider")
        self._critic(outsider, Decimal(100))
        self._list("Other").items.add(outsider)

        titles = self._titles("Best Owned - Xbox")
        response = self.client.get(reverse("dashboard_games"))

        self.assertEqual(
            titles,
            ["Bravo", "Alpha", "Also", "Mid", "Soon", "Fresh", "Amy", "Zed"],
        )
        self.assertContains(response, ">91<")
        self.assertNotContains(response, "91.4")
        self.assertNotContains(response, "91.2")

    def test_best_owned_uses_the_first_list_when_the_name_is_duplicated(self):
        """A second list with the same name does not replace the earlier one."""
        first = self._list("Xbox")
        second = self._list("Xbox")
        kept = self._item("kept", "Kept")
        ignored = self._item("ignored", "Ignored")
        first.items.add(kept)
        second.items.add(ignored)

        self.assertLess(first.id, second.id)
        self.assertEqual(self._titles("Best Owned - Xbox"), ["Kept"])

    def test_see_all_matches_the_row_past_the_cap(self):
        """The row stops at ten and See all keeps that order for every game."""
        xbox = self._list("Xbox")
        total = 11
        for number in range(total):
            item = self._item(f"owned-{number}", f"Owned {number:02d}")
            self._critic(item, 100 - number)
            xbox.items.add(item)

        dashboard = self.client.get(reverse("dashboard_games"))
        row = next(
            entry
            for entry in dashboard.context["rows"]
            if entry["title"] == "Best Owned - Xbox"
        )
        page = self.client.get(reverse("dashboard_row", args=["best-owned-xbox"]))
        shown = [entry.item.id for entry in page.context["entries"]]
        expected = [entry.item.id for entry in row["media"]]

        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(row["media"]), 10)
        self.assertEqual(shown[:10], expected)
        self.assertEqual(len(shown), total)
        self.assertEqual(
            [entry.item.title for entry in page.context["entries"]],
            [f"Owned {number:02d}" for number in range(total)],
        )
        self.assertNotContains(dashboard, "Owned 10")
        self.assertContains(page, "Owned 10")

    def test_missing_lists_leave_the_rows_empty(self):
        """No Xbox or Switch list is an empty page, not an error."""
        response = self.client.get(reverse("dashboard_games"))

        self.assertEqual(response.status_code, 200)
        for row in response.context["rows"]:
            self.assertEqual(row["media"], [])
        self.assertContains(response, "No games in progress.")
        self.assertContains(response, "No unfinished owned games.")

    def _titles(self, title):
        response = self.client.get(reverse("dashboard_games"))
        row = next(
            entry for entry in response.context["rows"] if entry["title"] == title
        )
        return [entry.item.title for entry in row["media"]]

    def _list(self, name):
        return CustomList.objects.create(name=name, owner=self.user)

    def _item(self, media_id, title):
        return Item.objects.create(
            media_id=media_id,
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title=title,
            image="http://example.com/a.jpg",
        )

    def _track(self, item, status):
        return Game.objects.create(item=item, user=self.user, status=status)

    def _critic(self, item, value):
        ItemRating.objects.create(
            item=item,
            metacritic_value=value,
            metacritic_normalized=value,
            ratings_updated_at=timezone.now(),
        )


class GameCriticScoreTests(TestCase):
    """IGDB aggregated_rating stored on the item's Metacritic fields."""

    def test_games_stay_out_of_the_movie_rating_types(self):
        """The nightly movie refresh still ignores games."""
        self.assertNotIn(MediaTypes.GAME.value, RATED_MEDIA_TYPES)

    @patch("app.ratings.igdb.critic_scores")
    def test_refresh_stores_scores_and_skips_a_fresh_second_pass(self, scores):
        """87.53 is stored as 87.5, a missing rating as NULL, then left alone."""
        movie = Item.objects.create(
            media_id="10",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="http://example.com/a.jpg",
        )
        scored = self._game("10", "Scored")
        missing = self._game("11", "Missing")
        scores.return_value = {10: 87.53, 11: None}

        self.assertEqual(refresh_game_critic_scores(), 2)

        scored_rating = ItemRating.objects.get(item=scored)
        self.assertEqual(scored_rating.metacritic_value, Decimal("87.5"))
        self.assertEqual(scored_rating.metacritic_normalized, Decimal("87.5"))
        self.assertIsNotNone(scored_rating.ratings_updated_at)
        missing_rating = ItemRating.objects.get(item=missing)
        self.assertIsNone(missing_rating.metacritic_value)
        self.assertIsNone(missing_rating.metacritic_normalized)
        self.assertIsNotNone(missing_rating.ratings_updated_at)
        self.assertFalse(ItemRating.objects.filter(item=movie).exists())
        scores.assert_called_once_with([10, 11])

        self.assertEqual(refresh_game_critic_scores(), 0)
        scores.assert_called_once()

    @patch("app.ratings.igdb.critic_scores", return_value={5: 80})
    def test_nightly_refresh_counts_the_games_it_saves(self, scores):
        """refresh_stale_ratings adds the game critic-score count."""
        self._game("5", "Portal")

        updated, deferred = refresh_stale_ratings()

        self.assertEqual(updated, 1)
        self.assertEqual(deferred, 0)
        self.assertEqual(
            ItemRating.objects.get().metacritic_value,
            Decimal("80.0"),
        )
        scores.assert_called_once_with([5])

    @patch("app.ratings.igdb.critic_scores")
    def test_a_failed_batch_is_skipped(self, scores):
        """An IGDB error logs and moves on without saving that batch."""
        self._game("12", "Offline")
        scores.side_effect = ProviderAPIError(Sources.IGDB.value, Exception("down"))

        self.assertEqual(refresh_game_critic_scores(), 0)

        self.assertFalse(ItemRating.objects.exists())

    @patch(
        "app.management.commands.refresh_game_critic_scores.refresh_game_critic_scores",
        return_value=0,
    )
    def test_command_defaults_to_stale_and_all_refreshes_everything(self, refresh):
        """The command refreshes stale scores unless --all is set."""
        call_command("refresh_game_critic_scores")
        refresh.assert_called_once_with(stale_only=True)

        refresh.reset_mock()
        call_command("refresh_game_critic_scores", "--all")

        refresh.assert_called_once_with(stale_only=False)

    def _game(self, media_id, title):
        return Item.objects.create(
            media_id=media_id,
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title=title,
            image="http://example.com/a.jpg",
        )


class IgdbCriticScoreTests(TestCase):
    """The IGDB aggregated_rating request."""

    def test_empty_input_does_not_call_igdb(self):
        """No ids means no request."""
        self.assertEqual(igdb.critic_scores([]), {})
        self.assertEqual(igdb.critic_scores(["nope", None]), {})

    @patch("app.providers.igdb.get_access_token", return_value="token")
    @patch("app.providers.igdb.services.api_request")
    def test_one_post_includes_ids_missing_from_the_response(self, api, _token):
        """The body asks for every id, and a gap in the response is None."""
        api.return_value = [
            {"id": 1, "aggregated_rating": 87.53},
            {"id": 2},
        ]

        result = igdb.critic_scores([1, 2, 3])

        self.assertEqual(result, {1: 87.53, 2: None, 3: None})
        api.assert_called_once()
        _args, kwargs = api.call_args
        self.assertEqual(
            kwargs["data"],
            "fields aggregated_rating; where id = (1,2,3); limit 3;",
        )
        self.assertEqual(_args[1], "POST")
        self.assertEqual(_args[2], "https://api.igdb.com/v4/games")
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))
        self.assertIn("Client-ID", kwargs["headers"])

    @patch("app.providers.igdb.get_access_token", side_effect=["old", "new"])
    @patch("app.providers.igdb.services.api_request")
    def test_unauthorized_retries_once(self, api, _token):
        """A 401 refreshes the token and tries the same query again."""
        denied = requests.Response()
        denied.status_code = 401
        denied.url = "https://api.igdb.com/v4/games"
        api.side_effect = [
            requests.HTTPError(response=denied),
            [{"id": 1, "aggregated_rating": 10}],
        ]

        result = igdb.critic_scores([1])

        self.assertEqual(result, {1: 10})
        self.assertEqual(api.call_count, 2)
