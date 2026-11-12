import json
import re
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from app.models import (
    Anime,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from events.models import Event
from users.models import HomeSortChoices


class HomeViewTests(TestCase):
    """Test the home view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.metadata_patcher = patch("app.providers.services.get_media_metadata")
        self.mock_get_media_metadata = self.metadata_patcher.start()
        self.addCleanup(self.metadata_patcher.stop)

        def mock_get_media_metadata(
            media_type,
            _media_id,
            _source,
            season_numbers=None,
            _episode_number=None,
        ):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "details": {"seasons": 1},
                    "related": {
                        "seasons": [
                            {
                                "season_number": 1,
                                "image": "http://example.com/image.jpg",
                            },
                        ],
                    },
                }

            if media_type == "tv_with_seasons":
                season_number = season_numbers[0]
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "details": {"seasons": 1},
                    f"season/{season_number}": {
                        "episodes": [{"id": i} for i in range(1, 11)],
                    },
                    "related": {
                        "seasons": [
                            {
                                "season_number": season_number,
                                "image": "http://example.com/image.jpg",
                            },
                        ],
                    },
                }

            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 10,
                    "season/1": {
                        "episodes": [{"id": i} for i in range(1, 11)],
                    },
                }

            if media_type == MediaTypes.ANIME.value:
                return {
                    "title": "Test Anime",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 24,
                }

            if media_type == MediaTypes.MOVIE.value:
                return {
                    "title": "Planned Movie",
                    "image": "http://example.com/image.jpg",
                    "max_progress": 1,
                }

            return {"max_progress": None}

        self.mock_get_media_metadata.side_effect = mock_get_media_metadata

        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        base_watched_at = timezone.now()
        for i in range(1, 6):  # Create 5 episodes
            episode_item = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Test TV Show",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=i,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=base_watched_at - timezone.timedelta(days=6 - i),
            )

        anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )
        Event.objects.create(
            item=anime_item,
            content_number=16,
            datetime=timezone.now() - timezone.timedelta(days=1),
        )
        Event.objects.create(
            item=anime_item,
            content_number=17,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

        movie_item = Item.objects.create(
            media_id="10",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Planned Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=season_item,
            content_number=6,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

    def test_home_view(self):
        """Test the home view displays in-progress and planning media."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")

        self.assertIn("home_sections", response.context)

        sections_by_key = {
            section["key"]: section for section in response.context["home_sections"]
        }
        self.assertIn(Status.IN_PROGRESS.value, sections_by_key)
        self.assertIn(Status.PLANNING.value, sections_by_key)

        in_progress_section = sections_by_key[Status.IN_PROGRESS.value]
        planning_section = sections_by_key[Status.PLANNING.value]

        self.assertIn(MediaTypes.SEASON.value, in_progress_section["media_types"])
        self.assertIn(MediaTypes.ANIME.value, in_progress_section["media_types"])
        self.assertIn(MediaTypes.MOVIE.value, planning_section["media_types"])

        self.assertIn("sort_choices", response.context)
        self.assertEqual(response.context["sort_choices"], HomeSortChoices.choices)
        self.assertEqual(in_progress_section["count"], 2)
        self.assertEqual(planning_section["count"], 1)

        season = in_progress_section["media_types"][MediaTypes.SEASON.value]
        self.assertEqual(len(season["items"]), 1)
        self.assertEqual(season["items"][0].progress, 5)

        planning_movies = planning_section["media_types"][MediaTypes.MOVIE.value]
        self.assertEqual(len(planning_movies["items"]), 1)
        self.assertEqual(planning_movies["items"][0].status, Status.PLANNING.value)

    def test_home_view_with_sort(self):
        """Test the home view with sorting parameter."""
        response = self.client.get(reverse("home") + "?sort=completion")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "completion")

        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, "completion")

    def test_home_view_hides_unreleased_when_filter_enabled(self):
        """Test home view can hide unreleased media in existing sections."""
        response = self.client.get(reverse("home") + "?hide_unreleased=true")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["hide_unreleased"])

        sections_by_key = {
            section["key"]: section for section in response.context["home_sections"]
        }
        self.assertIn(Status.IN_PROGRESS.value, sections_by_key)

        in_progress_section = sections_by_key[Status.IN_PROGRESS.value]
        planning_section = sections_by_key[Status.PLANNING.value]

        self.assertEqual(in_progress_section["count"], 1)
        self.assertIn(MediaTypes.ANIME.value, in_progress_section["media_types"])
        self.assertNotIn(MediaTypes.SEASON.value, in_progress_section["media_types"])
        self.assertEqual(planning_section["count"], 1)

        self.user.refresh_from_db()
        self.assertTrue(self.user.home_hide_unreleased)

    def test_home_view_keeps_in_progress_without_upcoming_release(self):
        """In-progress media without upcoming release stays when hiding unreleased."""
        movie_item = Item.objects.create(
            media_id="in-progress-no-release",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Caught Up Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        response = self.client.get(reverse("home") + "?hide_unreleased=true")

        self.assertEqual(response.status_code, 200)
        in_progress_section = next(
            section
            for section in response.context["home_sections"]
            if section["key"] == Status.IN_PROGRESS.value
        )
        movies = in_progress_section["media_types"][MediaTypes.MOVIE.value]["items"]
        self.assertTrue(
            any(media.item.media_id == "in-progress-no-release" for media in movies),
        )

    def test_home_view_hides_planning_with_upcoming_release(self):
        """Planning media with a scheduled future release is hidden when filtered."""
        upcoming_item = Item.objects.create(
            media_id="planning-with-release",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Upcoming Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=upcoming_item,
            user=self.user,
            status=Status.PLANNING.value,
        )
        Event.objects.create(
            item=upcoming_item,
            datetime=timezone.now() + timezone.timedelta(days=1),
        )

        response = self.client.get(reverse("home") + "?hide_unreleased=true")

        self.assertEqual(response.status_code, 200)
        planning_section = next(
            section
            for section in response.context["home_sections"]
            if section["key"] == Status.PLANNING.value
        )
        movies = planning_section["media_types"][MediaTypes.MOVIE.value]["items"]
        self.assertTrue(
            any(media.item.media_id == "10" for media in movies),
        )
        self.assertFalse(
            any(media.item.media_id == "planning-with-release" for media in movies),
        )

    def test_home_view_htmx_load_more_preserves_hide_unreleased(self):
        """Test HTMX load more applies hide unreleased filter."""
        self.user.home_hide_unreleased = True
        self.user.save(update_fields=["home_hide_unreleased"])

        for i in range(6, 36):
            anime_item = Item.objects.create(
                media_id=f"incoming-{i}",
                source=Sources.MAL.value,
                media_type=MediaTypes.ANIME.value,
                title=f"Incoming Anime {i}",
                image="http://example.com/image.jpg",
            )
            Anime.objects.create(
                item=anime_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
                progress=1,
            )
            Event.objects.create(
                item=anime_item,
                content_number=2,
                datetime=timezone.now() + timezone.timedelta(days=1),
            )

        response = self.client.get(
            reverse("home")
            + f"?load_status={Status.IN_PROGRESS.value}"
            + f"&load_media_type={MediaTypes.ANIME.value}",
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")
        self.assertEqual(response.context["home_status"], Status.IN_PROGRESS.value)
        self.assertEqual(len(response.context["media_list"]["items"]), 0)
        self.assertEqual(response.context["media_list"]["total"], 1)

    @patch("app.providers.services.get_media_metadata")
    def test_home_view_htmx_load_more(self, mock_get_media_metadata):
        """Test the HTMX load more functionality."""
        mock_get_media_metadata.return_value = {
            "title": "Test TV Show",
            "image": "http://example.com/image.jpg",
            "season/1": {
                "episodes": [{"id": 1}, {"id": 2}, {"id": 3}],  # 3 episodes
            },
            "related": {
                "seasons": [
                    {"season_number": 1, "image": "http://example.com/image.jpg"},
                ],  # Only one season
            },
        }

        for i in range(6, 20):  # Create 14 more TV shows (we already have 1)
            season_item = Item.objects.create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=f"Test TV Show {i}",
                image="http://example.com/image.jpg",
                season_number=1,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

            episode_item = Item.objects.create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Test TV Show {i}",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=1,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now(),
            )

        # Now test the load more functionality
        response = self.client.get(
            reverse("home") + "?load_media_type=season", headers={"hx-request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")

        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["home_status"], Status.IN_PROGRESS.value)

        self.assertIn("items", response.context["media_list"])
        self.assertIn("total", response.context["media_list"])

        # Since we're loading more (items after the first 14),
        # we should have at least 1 item in the response
        self.assertEqual(len(response.context["media_list"]["items"]), 1)
        self.assertEqual(
            response.context["media_list"]["total"],
            15,
        )  # 15 TV shows total

    def test_home_view_htmx_load_more_for_planning(self):
        """Test the HTMX load more functionality for planning media."""
        for i in range(1, 16):
            movie_item = Item.objects.create(
                media_id=f"planning-{i}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Planned Movie {i}",
                image="http://example.com/image.jpg",
            )
            Movie.objects.create(
                item=movie_item,
                user=self.user,
                status=Status.PLANNING.value,
            )

        response = self.client.get(
            reverse("home")
            + (
                f"?load_status={Status.PLANNING.value}"
                f"&load_media_type={MediaTypes.MOVIE.value}"
            ),
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")
        self.assertEqual(response.context["home_status"], Status.PLANNING.value)
        self.assertIn("media_list", response.context)
        self.assertEqual(len(response.context["media_list"]["items"]), 2)
        self.assertEqual(response.context["media_list"]["total"], 16)


def _anchor(html, href):
    """Return the class and inner HTML of the sidebar anchor for href."""
    match = re.search(
        rf'<a href="{re.escape(href)}"\s+class="([^"]*)">(.*?)</a>',
        html,
        re.DOTALL,
    )
    if match is None:
        msg = f"No anchor for {href}"
        raise AssertionError(msg)
    return match.group(1), match.group(2)


class RootRedirectTests(TestCase):
    """Site root opens the dashboard; upstream Home stays at /home."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_authenticated_root_redirects_to_dashboard(self):
        """GET / is a temporary redirect to the dashboard, which renders."""
        response = self.client.get("/")

        self.assertRedirects(
            response,
            reverse("dashboard"),
            status_code=302,
            target_status_code=200,
        )
        self.assertEqual(response.url, "/dashboard")
        self.assertNotIn("?", response.url)

        with_query = self.client.get("/?sort=completion")
        self.assertEqual(with_query.status_code, 302)
        self.assertEqual(with_query.url, reverse("dashboard"))

    def test_home_url_is_home_and_renders_the_home_template(self):
        """The home URL name stays on /home and is not the media-list pattern."""
        self.assertEqual(reverse("home"), "/home")
        self.assertEqual(resolve("/").url_name, "root")
        self.assertEqual(resolve("/home").url_name, "home")
        self.assertEqual(
            resolve(f"/{self.user.username}/{MediaTypes.MOVIE.value}").url_name,
            "medialist",
        )

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")
        self.assertContains(response, f'href="{reverse("home")}?sort=')

    def test_dashboard_renders(self):
        """GET /dashboard returns the dashboard page."""
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/dashboard.html")

    def test_anonymous_root_ends_at_login_then_dashboard(self):
        """A logged-out GET / reaches login, and signing in opens the dashboard."""
        self.client.logout()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))

        followed = self.client.get("/", follow=True)

        self.assertEqual(followed.status_code, 200)
        self.assertEqual(followed.resolver_match.url_name, "account_login")
        self.assertTrue(followed.redirect_chain)
        self.assertEqual(followed.redirect_chain[0][1], 302)
        next_url = parse_qs(urlparse(followed.redirect_chain[-1][0]).query)["next"][0]

        logged_in = self.client.post(
            reverse("account_login"),
            {
                "login": self.credentials["username"],
                "password": self.credentials["password"],
                "next": next_url,
            },
            follow=True,
        )

        self.assertEqual(logged_in.status_code, 200)
        self.assertEqual(logged_in.request["PATH_INFO"], reverse("dashboard"))

    def test_home_sidebar_is_active_only_on_home(self):
        """The Home item is active on /home and inactive on /dashboard."""
        home = self.client.get(reverse("home"))
        dashboard = self.client.get(reverse("dashboard"))
        home_html = home.content.decode()
        dashboard_html = dashboard.content.decode()

        home_class, home_inner = _anchor(home_html, reverse("home"))
        self.assertIn("bg-[#2c3136]", home_class)
        self.assertIn("text-indigo-400", home_inner)

        inactive_class, inactive_inner = _anchor(dashboard_html, reverse("home"))
        self.assertNotIn("bg-[#2c3136]", inactive_class)
        self.assertNotIn("text-indigo-400", inactive_inner)

        dashboard_class, dashboard_inner = _anchor(dashboard_html, reverse("dashboard"))
        self.assertIn("bg-[#2c3136]", dashboard_class)
        self.assertIn("text-indigo-400", dashboard_inner)

    def test_manifest_starts_at_dashboard_and_keeps_its_id(self):
        """The installed app identity stays /, and launch goes to the dashboard."""
        manifest_path = (
            Path(settings.BASE_DIR) / "static" / "favicon" / "site.webmanifest"
        )
        manifest = json.loads(manifest_path.read_text())

        self.assertEqual(manifest["start_url"], "/dashboard")
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["scope"], "/")
        home_shortcut = next(
            shortcut for shortcut in manifest["shortcuts"] if shortcut["name"] == "Home"
        )
        self.assertEqual(home_shortcut["url"], "/home")
        for shortcut in manifest["shortcuts"]:
            resolve(shortcut["url"])
