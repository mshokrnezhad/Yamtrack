from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app.models import Game, Item, MediaTypes, Movie, Sources, Status


def _item(media_id, media_type, title):
    return Item.objects.create(
        media_id=media_id,
        source=Sources.MANUAL.value,
        media_type=media_type,
        title=title,
        image="http://example.com/a.jpg",
    )


@override_settings(
    PUBLIC_PROFILE_USER="owner",
    PUBLIC_PROFILE_NAME="Owner Name",
    PUBLIC_PROFILE_INTRO="Mostly late at night.",
)
class PublicSummaryTests(TestCase):
    """The read-only summary that logged-out visitors see at /."""

    def setUp(self):
        """Create the owner with one game in progress and one finished movie."""
        cache.clear()
        self.owner = get_user_model().objects.create_user(
            username="owner",
            password="12345",  # noqa: S106
        )
        Game.objects.create(
            item=_item("1", MediaTypes.GAME.value, "Playing Game"),
            user=self.owner,
            status=Status.IN_PROGRESS.value,
            progress=90,
        )
        Movie.objects.create(
            item=_item("2", MediaTypes.MOVIE.value, "Finished Movie"),
            user=self.owner,
            status=Status.COMPLETED.value,
            score=9,
            end_date=timezone.now() - timedelta(days=1),
            notes="private note",
        )

    def test_visitor_sees_the_summary_without_private_details(self):
        """Name, intro, rows and score show; notes and item links do not."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Owner Name")
        self.assertContains(response, "Mostly late at night.")
        self.assertContains(response, "Playing Game")
        self.assertContains(response, "1h 30min")
        self.assertContains(response, "Finished Movie")
        self.assertContains(response, "★ 9")
        self.assertContains(response, reverse("account_login"))
        self.assertContains(response, "TMDB")
        self.assertNotContains(response, "private note")
        self.assertNotContains(response, "/details/")
        self.assertNotContains(response, "Register")

    def test_library_links_follow_the_private_profile_switch(self):
        """Shelf links appear only while the profile is public."""
        library = reverse(
            "medialist",
            kwargs={"username": "owner", "media_type": MediaTypes.MOVIE.value},
        )
        self.owner.profile_private = False
        self.owner.save()
        self.assertContains(self.client.get("/"), f'href="{library}"')

        cache.clear()
        self.owner.profile_private = True
        self.owner.save()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{library}"')

    def test_summary_is_cached(self):
        """A second visit reuses the rendered page."""
        self.client.get("/")
        Game.objects.update(progress=0)

        self.assertContains(self.client.get("/"), "1h 30min")

    def test_owner_still_lands_on_home(self):
        """Logged in, / keeps opening the dashboard."""
        self.client.force_login(self.owner)

        self.assertRedirects(self.client.get("/"), reverse("dashboard"))

    @override_settings(PUBLIC_PROFILE_USER="")
    def test_switched_off_sends_visitors_to_login(self):
        """Without PUBLIC_PROFILE_USER the root asks for login as before."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("account_login")))

    @override_settings(PUBLIC_PROFILE_USER="nobody")
    def test_unknown_user_sends_visitors_to_login(self):
        """A typo in PUBLIC_PROFILE_USER does not error."""
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
