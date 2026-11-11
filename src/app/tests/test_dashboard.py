from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import formats, timezone

from app.dashboard import (
    ROW_LIMIT,
    _caught_up_row,
    _list_row,
    _planning_row,
    build_rows,
    complete_finished_shows,
    needs_release_dates,
    page,
    refresh_for_user,
)
from app.models import (
    TV,
    Episode,
    Item,
    ItemRating,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.providers.services import ProviderAPIError
from app.templatetags.app_tags import compact_date
from events.models import Event
from lists.models import CustomList

METADATA = {
    "title": "Test",
    "image": "http://example.com/a.jpg",
    "max_progress": 1,
    "related": {"seasons": []},
}


class DashboardRowTests(TestCase):
    """Selection and ordering for each dashboard row."""

    def setUp(self):
        """Create two users and keep TV saves off the network."""
        self.user = get_user_model().objects.create_user(
            username="dash",
            password="test-pass",  # noqa: S106
        )
        self.other = get_user_model().objects.create_user(
            username="other",
            password="test-pass",  # noqa: S106
        )
        self.metadata = patch(
            "app.providers.services.get_media_metadata",
            return_value=METADATA,
        )
        self.releases = patch("app.models.Item.fetch_releases")
        self.metadata.start()
        self.releases.start()
        self.addCleanup(self.metadata.stop)
        self.addCleanup(self.releases.stop)

    def test_downloaded_movies_follow_public_score_and_ignore_other_lists(self):
        """The user's Downloaded movies sort by public score, nulls last."""
        own = self._list(self.user, "Downloaded")
        theirs = self._list(self.other, "Downloaded")
        high = self._movie_item("1", "High")
        low = self._movie_item("2", "Low")
        missing = self._movie_item("3", "Missing")
        untracked = self._movie_item("4", "Untracked")
        show = self._tv("5", "Show", Status.PLANNING.value)
        foreign = self._movie_item("6", "Foreign")
        self._score(high, 90, votes=5000)
        self._score(low, 40, votes=5000)
        self._score(untracked, 70, votes=5000)
        own.items.add(high, low, missing, untracked, show.item)
        theirs.items.add(foreign)
        Movie.objects.create(
            item=low,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        media = _list_row(
            self.user,
            own,
            MediaTypes.MOVIE.value,
            "Top Downloaded movies",
        )["media"]

        self.assertEqual(
            [entry.item.title for entry in media],
            ["High", "Untracked", "Low", "Missing"],
        )
        self.assertIsNone(media[-1].public_rating)
        untracked_card = next(entry for entry in media if entry.item.id == untracked.id)
        self.assertIsNone(untracked_card.status)

    def test_downloaded_movies_are_one_query_for_the_items_and_one_for_tracking(self):
        """A Downloaded movie row does not look up each title on its own."""
        own = self._list(self.user, "Downloaded")
        item = self._movie_item("7", "Once")
        own.items.add(item)

        with self.assertNumQueries(2):
            _list_row(self.user, own, MediaTypes.MOVIE.value, "Top Downloaded movies")

    def test_planning_movies_sort_by_public_score_and_cap_at_ten(self):
        """Only Planning movies are included, highest score first, ten at most."""
        kept = []
        for number in range(1, ROW_LIMIT + 1):
            item = self._movie_item(str(number), f"Movie {number:02d}")
            self._score(item, number * 5, votes=5000)
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.PLANNING.value,
            )
            kept.append(item.title)
        dropped = self._movie_item("99", "Dropped")
        self._score(dropped, 1, votes=5000)
        Movie.objects.create(
            item=dropped,
            user=self.user,
            status=Status.PLANNING.value,
        )
        completed = self._movie_item("100", "Done")
        self._score(completed, 100, votes=5000)
        Movie.objects.create(
            item=completed,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        Movie.objects.create(
            item=self._movie_item("101", "Theirs"),
            user=self.other,
            status=Status.PLANNING.value,
        )

        titles = self._titles(
            _planning_row(self.user, MediaTypes.MOVIE.value, "Top Planning movies"),
        )

        self.assertEqual(titles, list(reversed(kept)))
        self.assertNotIn("Dropped", titles)
        self.assertNotIn("Done", titles)
        self.assertNotIn("Theirs", titles)

    def test_low_vote_public_score_sorts_last(self):
        """A score under the vote minimum is treated as missing."""
        popular = self._movie_item("110", "Popular")
        thin = self._movie_item("111", "Thin")
        self._score(popular, 40, votes=1000)
        self._score(thin, 90, votes=10)
        for item in (popular, thin):
            Movie.objects.create(
                item=item,
                user=self.user,
                status=Status.PLANNING.value,
            )

        titles = self._titles(
            _planning_row(self.user, MediaTypes.MOVIE.value, "Top Planning movies"),
        )

        self.assertEqual(titles, ["Popular", "Thin"])

    def test_planning_movies_use_one_query(self):
        """The Planning movie row is a single sorted query."""
        item = self._movie_item("120", "Solo")
        Movie.objects.create(item=item, user=self.user, status=Status.PLANNING.value)

        with self.assertNumQueries(1):
            _planning_row(self.user, MediaTypes.MOVIE.value, "Top Planning movies")

    def test_downloaded_tv_is_separate_from_movies(self):
        """Downloaded TV uses the same list and the same score order."""
        own = self._list(self.user, "Downloaded")
        high = self._tv("201", "High Show", Status.IN_PROGRESS.value)
        low = self._tv("202", "Low Show", Status.PLANNING.value)
        movie = self._movie_item("203", "A Movie")
        self._score(high.item, 80, votes=2000)
        self._score(low.item, 20, votes=2000)
        self._score(movie, 99, votes=2000)
        own.items.add(high.item, low.item, movie)

        titles = self._titles(
            _list_row(self.user, own, MediaTypes.TV.value, "Top Downloaded TV"),
        )

        self.assertEqual(titles, ["High Show", "Low Show"])

    def test_planning_tv_excludes_other_statuses(self):
        """Only TV the user has marked Planning is in that row."""
        planning = self._tv("210", "Plan", Status.PLANNING.value)
        started = self._tv("211", "Started", Status.IN_PROGRESS.value)
        self._score(started.item, 90, votes=2000)
        self._score(planning.item, 30, votes=2000)

        titles = self._titles(
            _planning_row(self.user, MediaTypes.TV.value, "Top Planning TV"),
        )

        self.assertEqual(titles, ["Plan"])

    def test_caught_up_requires_every_aired_episode_and_sorts_by_last_watch(self):
        """A show is caught up only when no aired episode is still unwatched."""
        now = timezone.now()
        self._show_with_episode("301", "Current", now - timedelta(days=1))
        self._show_with_episode("302", "Older", now - timedelta(days=5))
        behind = self._tv("303", "Behind", Status.IN_PROGRESS.value)
        season = self._season(behind, 1)
        self._air(season, 1, now - timedelta(days=2))
        waiting = self._tv("304", "Waiting", Status.IN_PROGRESS.value)
        future = self._season(waiting, 1)
        self._air(future, 1, now + timedelta(days=3))
        finished = self._show_with_episode("305", "Finished", now)
        TV.objects.filter(pk=finished.pk).update(status=Status.COMPLETED.value)
        specials = self._show_with_episode("306", "Specials", now - timedelta(days=2))
        special = self._season(specials, 0)
        self._air(special, 1, now - timedelta(days=2))

        titles = self._titles(_caught_up_row(self.user))

        self.assertEqual(titles, ["Current", "Specials", "Older"])
        self.assertNotIn("Behind", titles)
        self.assertNotIn("Waiting", titles)
        self.assertNotIn("Finished", titles)

    def test_caught_up_keeps_a_recent_watch_or_air_date(self):
        """Keep a show with a recent watch or air date, and drop an older one."""
        now = timezone.now()
        self._show_with_episode("360", "Watched", now - timedelta(days=2))
        aired = self._tv("361", "Aired", Status.IN_PROGRESS.value)
        season = self._season(aired, 1)
        self._air(season, 1, now - timedelta(days=4))
        self._watch(season, 1, now - timedelta(days=90))
        stale = self._show_with_episode("362", "Stale", now - timedelta(days=90))
        special = self._season(stale, 0)
        self._air(special, 1, now - timedelta(days=1))
        self._watch(special, 1, now)

        row = _caught_up_row(self.user)

        self.assertEqual(self._titles(row), ["Watched", "Aired"])
        self.assertIn("status=In+progress", row["see_all"])
        self.assertNotIn("sort=", row["see_all"])
        stale.refresh_from_db()
        self.assertEqual(stale.status, Status.IN_PROGRESS.value)
        context = page(self.user)
        self.assertFalse(context["caught_up_quiet"])
        self.assertFalse(context["release_dates_pending"])

    def test_old_caught_up_show_is_hidden_and_stays_in_progress(self):
        """A show outside the window leaves the row without changing status."""
        watched = timezone.now() - timedelta(days=90)
        show = self._show_with_episode("363", "Archive", watched)

        self.assertNotIn("Caught up", [row["title"] for row in build_rows(self.user)])
        show.refresh_from_db()
        self.assertEqual(show.status, Status.IN_PROGRESS.value)
        context = page(self.user)
        self.assertTrue(context["caught_up_quiet"])
        self.assertFalse(context["release_dates_pending"])
        self.assertEqual(context["caught_up_days"], 60)

    def test_caught_up_cards_include_the_public_score(self):
        """Each caught-up card carries the annotated public score."""
        show = self._show_with_episode("311", "Scored", timezone.now())
        self._score(show.item, 80, votes=2000)

        row = _caught_up_row(self.user)

        self.assertEqual(len(row["media"]), 1)
        self.assertEqual(row["media"][0].public_rating, Decimal("80.0"))

    def test_finished_shows_move_to_completed_when_caught_up(self):
        """Ended or canceled shows with every aired episode watched are completed."""
        for tmdb_status in ("Ended", "Canceled"):
            with self.subTest(tmdb_status=tmdb_status):
                show = self._show_with_episode(
                    tmdb_status,
                    tmdb_status,
                    timezone.now(),
                )
                with self._tmdb_status(tmdb_status):
                    complete_finished_shows(self.user)
                show.refresh_from_db()
                self.assertEqual(show.status, Status.COMPLETED.value)

    def test_still_airing_show_stays_in_progress_and_in_caught_up(self):
        """A show that is still airing stays In progress and remains caught up."""
        show = self._show_with_episode("340", "Airing", timezone.now())

        with self._tmdb_status("Returning Series"):
            complete_finished_shows(self.user)

        show.refresh_from_db()
        self.assertEqual(show.status, Status.IN_PROGRESS.value)
        self.assertEqual(self._titles(_caught_up_row(self.user)), ["Airing"])

    def test_ended_show_with_an_unwatched_episode_stays_in_progress(self):
        """One aired episode left unwatched keeps the show In progress."""
        show = self._tv("341", "Behind", Status.IN_PROGRESS.value)
        season = self._season(show, 1)
        self._air(season, 1, timezone.now() - timedelta(days=1))

        with self._tmdb_status("Ended"):
            complete_finished_shows(self.user)

        show.refresh_from_db()
        self.assertEqual(show.status, Status.IN_PROGRESS.value)

    def test_unwatched_special_does_not_block_completion(self):
        """Season 0 does not keep an otherwise finished show In progress."""
        show = self._show_with_episode("342", "Specials", timezone.now())
        special = self._season(show, 0)
        self._air(special, 1, timezone.now() - timedelta(days=1))

        with self._tmdb_status("Ended"):
            complete_finished_shows(self.user)

        show.refresh_from_db()
        self.assertEqual(show.status, Status.COMPLETED.value)

    def test_ended_show_with_no_aired_episodes_stays_in_progress(self):
        """A show with no aired episodes is not treated as finished."""
        show = self._tv("343", "Empty", Status.IN_PROGRESS.value)

        with self._tmdb_status("Ended"):
            complete_finished_shows(self.user)

        show.refresh_from_db()
        self.assertEqual(show.status, Status.IN_PROGRESS.value)

    def test_missing_tmdb_status_leaves_the_show_in_progress(self):
        """A failed TMDB lookup does not complete the show."""
        show = self._show_with_episode("345", "Unknown", timezone.now())

        with patch(
            "app.providers.services.get_media_metadata",
            side_effect=ProviderAPIError(Sources.TMDB.value, Exception("down")),
        ):
            complete_finished_shows(self.user)

        show.refresh_from_db()
        self.assertEqual(show.status, Status.IN_PROGRESS.value)

    def test_refresh_reloads_the_calendar_before_the_status_check(self):
        """Release dates are loaded before a show can move to Completed."""
        show = self._show_with_episode("344", "Ordered", timezone.now())
        order = []

        def reload(_user):
            order.append("reload")

        def metadata(*_args, **_kwargs):
            order.append("status")
            return {**METADATA, "details": {"status": "Ended"}}

        with (
            patch("events.tasks.reload_calendar", side_effect=reload),
            patch(
                "app.providers.services.get_media_metadata",
                side_effect=metadata,
            ),
        ):
            refresh_for_user(self.user)

        self.assertEqual(order, ["reload", "status"])
        show.refresh_from_db()
        self.assertEqual(show.status, Status.COMPLETED.value)

    def _tmdb_status(self, status):
        return patch(
            "app.providers.services.get_media_metadata",
            return_value={**METADATA, "details": {"status": status}},
        )

    def test_in_progress_tv_without_events_needs_release_dates(self):
        """No season events means Caught up stays hidden and the hint applies."""
        show = self._tv("320", "No Dates", Status.IN_PROGRESS.value)

        self.assertTrue(needs_release_dates(self.user))
        self.assertNotIn("Caught up", [row["title"] for row in build_rows(self.user)])

        season = self._season(show, 1)
        self._air(season, 1, timezone.now() - timedelta(days=1))
        self._watch(season, 1, timezone.now())

        self.assertFalse(needs_release_dates(self.user))

    def test_caught_up_loads_episodes_without_a_query_per_show(self):
        """Caught up is the show query plus the season and episode prefetches."""
        self._show_with_episode("310", "Ready", timezone.now())

        with self.assertNumQueries(3):
            _caught_up_row(self.user)

    def test_empty_rows_are_left_out(self):
        """A row with nothing in it is not returned."""
        item = self._movie_item("400", "Only")
        Movie.objects.create(item=item, user=self.user, status=Status.PLANNING.value)

        rows = build_rows(self.user)

        self.assertEqual([row["title"] for row in rows], ["Top Planning movies"])

    def _titles(self, row):
        return [entry.item.title for entry in row["media"]]

    def _list(self, user, name):
        return CustomList.objects.create(name=name, owner=user)

    def _movie_item(self, media_id, title):
        return Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image="http://example.com/a.jpg",
        )

    def _score(self, item, normalized, *, votes):
        ItemRating.objects.create(
            item=item,
            imdb_value=Decimal(normalized) / 10,
            imdb_normalized=Decimal(normalized),
            imdb_votes=votes,
        )

    def _tv(self, media_id, title, status):
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="http://example.com/a.jpg",
        )
        return TV.objects.create(item=item, user=self.user, status=status)

    def _season(self, tv, number):
        item = Item.objects.create(
            media_id=tv.item.media_id,
            source=tv.item.source,
            media_type=MediaTypes.SEASON.value,
            title=tv.item.title,
            image=tv.item.image,
            season_number=number,
        )
        return Season.objects.create(
            item=item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

    def _air(self, season, episode_number, when):
        Event.objects.create(
            item=season.item,
            content_number=episode_number,
            datetime=when,
        )

    def _watch(self, season, episode_number, when):
        item = Item.objects.create(
            media_id=season.item.media_id,
            source=season.item.source,
            media_type=MediaTypes.EPISODE.value,
            title=season.item.title,
            image=season.item.image,
            season_number=season.item.season_number,
            episode_number=episode_number,
        )
        Episode.objects.bulk_create(
            [Episode(item=item, related_season=season, end_date=when)],
        )

    def _show_with_episode(self, media_id, title, watched_at):
        tv = self._tv(media_id, title, Status.IN_PROGRESS.value)
        season = self._season(tv, 1)
        self._air(season, 1, watched_at - timedelta(days=1))
        self._watch(season, 1, watched_at)
        return tv


class DashboardViewTests(TestCase):
    """The dashboard page, its links, and the login redirect."""

    def setUp(self):
        """Log in a user with one Planning movie."""
        self.user = get_user_model().objects.create_user(
            username="dash-view",
            password="test-pass",  # noqa: S106
        )
        self.client.login(username="dash-view", password="test-pass")  # noqa: S106
        cache.delete(f"dashboard_refresh_{self.user.pk}")
        item = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Visible",
            image="http://example.com/a.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
        )

    def test_page_shows_filled_rows_and_their_see_all_links(self):
        """Filled rows render, empty rows do not, and See all points at the filter."""
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Top Planning movies")
        self.assertContains(response, "Visible")
        self.assertContains(response, "See all")
        self.assertContains(response, "status=Planning")
        self.assertContains(response, "sort=public_rating")
        self.assertNotContains(response, "Top Downloaded movies")
        self.assertNotContains(response, "Caught up")
        self.assertContains(response, "dashboard-row")
        self.assertContains(response, 'href="/dashboard"')
        self.assertContains(response, "Refresh")
        self.assertNotContains(response, "release dates load")

    def test_page_hints_when_in_progress_tv_has_no_release_dates(self):
        """The hint shows for in-progress TV that has no season events yet."""
        with (
            patch("app.providers.services.get_media_metadata", return_value=METADATA),
            patch("app.models.Item.fetch_releases"),
        ):
            item = Item.objects.create(
                media_id="9",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Waiting",
                image="http://example.com/a.jpg",
            )
            TV.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(
            response,
            "last 60 days appear here after release dates load",
        )
        self.assertNotContains(response, ">Caught up<")

    def test_old_caught_up_show_explains_the_window(self):
        """An older caught-up show stays off the row and the page says why."""
        with (
            patch("app.providers.services.get_media_metadata", return_value=METADATA),
            patch("app.models.Item.fetch_releases"),
        ):
            item = Item.objects.create(
                media_id="363",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Archive",
                image="http://example.com/a.jpg",
            )
            show = TV.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                score=8,
            )
            season_item = Item.objects.create(
                media_id="363",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Archive",
                image="http://example.com/a.jpg",
                season_number=1,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=show,
                status=Status.IN_PROGRESS.value,
            )
            watched = timezone.now() - timedelta(days=90)
            Event.objects.create(
                item=season_item,
                content_number=1,
                datetime=watched - timedelta(days=1),
            )
            episode_item = Item.objects.create(
                media_id="363",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Archive",
                image="http://example.com/a.jpg",
                season_number=1,
                episode_number=1,
            )
            Episode.objects.bulk_create(
                [Episode(item=episode_item, related_season=season, end_date=watched)],
            )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "No shows caught up in the last 60 days")
        self.assertContains(response, "Older shows stay In progress")
        self.assertNotContains(response, ">Caught up<")
        self.assertNotContains(response, "release dates load")

    def test_dashboard_card_uses_a_short_date_beside_the_score(self):
        """The date badge is short enough to share the card with the score star."""
        watched = timezone.now() - timedelta(days=1)
        with (
            patch("app.providers.services.get_media_metadata", return_value=METADATA),
            patch("app.models.Item.fetch_releases"),
        ):
            item = Item.objects.create(
                media_id="364",
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                title="Recent",
                image="http://example.com/a.jpg",
            )
            show = TV.objects.create(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                score=8,
            )
            season_item = Item.objects.create(
                media_id="364",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title="Recent",
                image="http://example.com/a.jpg",
                season_number=1,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=show,
                status=Status.IN_PROGRESS.value,
            )
            Event.objects.create(
                item=season_item,
                content_number=1,
                datetime=watched - timedelta(days=1),
            )
            episode_item = Item.objects.create(
                media_id="364",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Recent",
                image="http://example.com/a.jpg",
                season_number=1,
                episode_number=1,
            )
            Episode.objects.bulk_create(
                [Episode(item=episode_item, related_season=season, end_date=watched)],
            )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, compact_date(watched))
        self.assertNotContains(
            response,
            timezone.localtime(watched).strftime("%Y-%m-%d"),
        )
        self.assertNotContains(response, "sm:right-2")
        self.assertContains(response, "My rating")

    def test_compact_date_drops_the_year_only_for_this_year(self):
        """This year is month and day. Another year keeps the year and drops the day."""
        today = timezone.localtime()
        older = today - timedelta(days=400)

        self.assertEqual(compact_date(today), formats.date_format(today, "M j"))
        self.assertNotIn(str(today.year), compact_date(today))
        self.assertEqual(compact_date(older), formats.date_format(older, "M Y"))
        self.assertIn(str(older.year), compact_date(older))

    @patch("app.tasks.refresh_dashboard.delay")
    def test_refresh_queues_the_job_and_says_it_is_refreshing(self, delay):
        """Refresh queues the user's job and shows that it is running."""
        delay.return_value.id = "task-1"

        response = self.client.post(reverse("dashboard_refresh"))

        self.assertContains(response, "Refreshing")
        delay.assert_called_once_with(self.user.pk)
        self.assertEqual(cache.get(f"dashboard_refresh_{self.user.pk}"), "task-1")

    @patch("app.tasks.refresh_dashboard.delay")
    def test_refresh_is_limited_to_once_a_minute(self, delay):
        """A second refresh in the same minute does not queue another job."""
        delay.return_value.id = "task-1"
        self.client.post(reverse("dashboard_refresh"))
        delay.return_value.id = "task-2"

        response = self.client.post(reverse("dashboard_refresh"))

        self.assertContains(response, "Refresh the page in a minute.")
        delay.assert_called_once_with(self.user.pk)

    @patch("app.views.AsyncResult")
    def test_refresh_reloads_the_page_when_the_job_finishes(self, result):
        """A finished job tells the page to reload."""
        cache.set(f"dashboard_refresh_{self.user.pk}", "task-1", timeout=60)
        result.return_value.ready.return_value = True

        response = self.client.get(
            reverse("dashboard_refresh_status", args=["task-1"]),
        )

        self.assertEqual(response["HX-Refresh"], "true")

    @patch("app.views.AsyncResult")
    def test_refresh_status_waits_while_the_job_is_running(self, result):
        """A running job keeps the Refreshing message in place."""
        cache.set(f"dashboard_refresh_{self.user.pk}", "task-1", timeout=60)
        result.return_value.ready.return_value = False

        response = self.client.get(
            reverse("dashboard_refresh_status", args=["task-1"]),
        )

        self.assertEqual(response.status_code, 204)

    def test_anonymous_visitors_are_sent_to_login(self):
        """The dashboard is the private landing page."""
        self.client.logout()

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)
