"""Read-only summary of one user's shelves, shown to visitors who are not logged in.

Nothing here links to item pages or exposes notes, history, or exact watch
times: those stay behind login. Dates are shown as month and year only.
"""

from datetime import date

from django.conf import settings
from django.db.models import F, Max
from django.utils import timezone
from django.utils.translation import gettext as _

from app import dashboard
from app import statistics as stats
from app.models import TV, Book, Game, MediaTypes, Movie, Status
from users.models import User

ROW_LIMIT = 8
FINISHED_LIMIT = 12
BEST_LIMIT = 10
CACHE_SECONDS = 600


def public_user():
    """Return the user whose summary is public, or None when it is switched off.

    The summary is off unless PUBLIC_PROFILE_USER names an existing user. Setting
    it is the opt-in; the private-profile switch only hides the library links.
    """
    username = settings.PUBLIC_PROFILE_USER
    if not username:
        return None
    return User.objects.filter(username=username).first()


def cache_key(user):
    """Return the cache key for this user's rendered summary."""
    return f"public-summary:{user.pk}"


def summary(user):
    """Return everything the public page shows for this user."""
    today = timezone.localdate()
    user_media, media_count = stats.get_user_media(
        user,
        date(today.year, 1, 1),
        today,
    )
    _distribution, top_rated = stats.get_score_distribution(user_media)
    enabled = set(user.get_enabled_media_types())
    return {
        "name": settings.PUBLIC_PROFILE_NAME or user.username,
        "intro": settings.PUBLIC_PROFILE_INTRO,
        "year": today.year,
        "updated": today,
        "marquee_items": dashboard.marquee_items(user),
        "currently": _currently(user, enabled),
        "finished": _recently_finished(user),
        "best": top_rated[:BEST_LIMIT],
        "numbers": stats.get_consumption_stats(user_media, media_count),
        "shelves": [
            {
                "media_type": media_type,
                "username": user.username,
            }
            for media_type in (
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.GAME.value,
                MediaTypes.BOOK.value,
            )
            if media_type in enabled and not user.profile_private
        ],
    }


def _currently(user, enabled):
    """Return the watching, reading and playing rows that have something in them."""
    rows = []
    if MediaTypes.TV.value in enabled:
        shows = dashboard.continue_watching(user)[:ROW_LIMIT]
        rows.append(
            {
                "key": "watching",
                "media_type": MediaTypes.TV.value,
                "media": [
                    {
                        "item": show.item,
                        "detail": _episode_detail(show),
                    }
                    for show in shows
                ],
            },
        )
    if MediaTypes.BOOK.value in enabled:
        books = dashboard.currently_reading(user)[:ROW_LIMIT]
        rows.append(
            {
                "key": "reading",
                "media_type": MediaTypes.BOOK.value,
                "media": [
                    {
                        "item": book.item,
                        "detail": _pages_detail(book),
                    }
                    for book in books
                ],
            },
        )
    if MediaTypes.GAME.value in enabled:
        games = (
            Game.objects.filter(user=user, status=Status.IN_PROGRESS.value)
            .select_related("item")
            .order_by(F("progressed_at").desc(nulls_last=True), "-created_at")[
                :ROW_LIMIT
            ]
        )
        rows.append(
            {
                "key": "playing",
                "media_type": MediaTypes.GAME.value,
                "media": [
                    {
                        "item": game.item,
                        "detail": game.formatted_progress if game.progress else "",
                    }
                    for game in games
                ],
            },
        )
    return [row for row in rows if row["media"]]


def _episode_detail(show):
    next_episode = getattr(show, "next_episode", None)
    if not next_episode:
        return ""
    left = _("%(count)s left") % {"count": show.episodes_remaining}
    return f"{next_episode} · {left}"


def _pages_detail(book):
    if book.max_progress:
        return f"{book.progress} / {book.max_progress}"
    return str(book.progress)


def _recently_finished(user):
    """Return the latest completed movies, shows, games and books."""
    completed = Status.COMPLETED.value
    finished = []
    for model in (Movie, Game, Book):
        finished.extend(
            model.objects.filter(user=user, status=completed, end_date__isnull=False)
            .select_related("item")
            .annotate(finished_at=F("end_date"))
            .order_by("-end_date")[:FINISHED_LIMIT],
        )
    # A show's finish date is its last watched episode.
    finished.extend(
        TV.objects.filter(user=user, status=completed)
        .annotate(finished_at=Max("seasons__episodes__end_date"))
        .filter(finished_at__isnull=False)
        .select_related("item")
        .order_by("-finished_at")[:FINISHED_LIMIT],
    )
    finished.sort(key=lambda media: media.finished_at, reverse=True)
    return finished[:FINISHED_LIMIT]
