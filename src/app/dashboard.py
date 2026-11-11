import logging

from django.core.cache import cache
from django.db.models import (
    Exists,
    F,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Window,
    prefetch_related_objects,
)
from django.db.models.functions import Lower, RowNumber
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.utils import bulk_update_with_history

from app.models import TV, Episode, Item, MediaTypes, Movie, Season, Sources, Status
from app.providers import services
from app.ratings import annotate_public_rating
from events.models import Event
from lists.models import CustomList

logger = logging.getLogger(__name__)

ROW_LIMIT = 10
PAGE_SIZE = 32
DOWNLOADED_LIST = "Downloaded"
REFRESH_COOLDOWN = 60
FINISHED_TMDB_STATUSES = frozenset({"Ended", "Canceled"})
_UNSET = object()
_ROWS = {
    "top-downloaded-movies": ("downloaded", MediaTypes.MOVIE.value),
    "top-planning-movies": ("planning", MediaTypes.MOVIE.value),
    "top-downloaded-tv": ("downloaded", MediaTypes.TV.value),
    "top-planning-tv": ("planning", MediaTypes.TV.value),
    "continue-watching": ("continue", MediaTypes.TV.value),
}


class _ListedMedia:
    """Card stand-in for a list item the user has not tracked."""

    def __init__(self, item, public_rating):
        self.item = item
        self.public_rating = public_rating
        self.status = None
        self.score = None
        self.progress = 0
        self.repeats = 0
        self.end_date = None
        self.start_date = None


def _title(key):
    """Return the heading for a dashboard row."""
    titles = {
        "top-downloaded-movies": _("Top Downloaded movies"),
        "top-planning-movies": _("Top Planning movies"),
        "top-downloaded-tv": _("Top Downloaded TV"),
        "top-planning-tv": _("Top Planning TV"),
        "continue-watching": _("Continue watching"),
    }
    return titles[key]


def _key(kind, media_type):
    """Return the route key for a row kind and media type."""
    for key, row in _ROWS.items():
        if row == (kind, media_type):
            return key
    message = "Unknown dashboard row"
    raise KeyError(message)


def build_rows(user):
    """Return the dashboard rows that have something to show."""
    downloaded = _downloaded_list(user)
    rows = [
        _list_row(
            user,
            downloaded,
            MediaTypes.MOVIE.value,
            _title("top-downloaded-movies"),
        ),
        _planning_row(user, MediaTypes.MOVIE.value, _title("top-planning-movies")),
        _list_row(user, downloaded, MediaTypes.TV.value, _title("top-downloaded-tv")),
        _planning_row(user, MediaTypes.TV.value, _title("top-planning-tv")),
    ]
    rows = [row for row in rows if row["media"]]
    rows.append(_continue_row(user))
    return rows


def row_page(user, key, page):
    """Return one page of the same ordered items as that dashboard row."""
    if key not in _ROWS:
        return None
    records, has_next, number = _slice_page(_ordered(user, key), page)
    return {
        "key": key,
        "title": _title(key),
        "media_type": _ROWS[key][1],
        "entries": _cards(user, key, records),
        "has_next": has_next,
        "next_page": number + 1,
    }


def _ordered(user, key, *, downloaded=_UNSET):
    """Return the ordered queryset for a row, with no length limit.

    The dashboard row and its See all page both call this.
    """
    kind, media_type = _ROWS[key]
    if kind == "downloaded":
        if downloaded is _UNSET:
            downloaded = _downloaded_list(user)
        return _downloaded_items(user, downloaded, media_type)
    if kind == "planning":
        return _planning_entries(user, media_type)
    return _continue_shows(user)


def _slice_page(queryset, page):
    """Return one page and whether another follows, without a count query."""
    number = _page_number(page)
    start = (number - 1) * PAGE_SIZE
    window = list(queryset[start : start + PAGE_SIZE + 1])
    return window[:PAGE_SIZE], len(window) > PAGE_SIZE, number


def _page_number(page):
    """Return a page number of at least 1."""
    try:
        number = int(page)
    except (TypeError, ValueError):
        return 1
    if number < 1:
        return 1
    return number


def _downloaded_list(user):
    """Return the user's Downloaded list, if they have one."""
    return (
        CustomList.objects.filter(owner=user, name=DOWNLOADED_LIST)
        .order_by("id")
        .first()
    )


def _list_row(user, custom_list, media_type, title):
    """Movies or shows in the Downloaded list, highest public score first."""
    key = _key("downloaded", media_type)
    if custom_list is None:
        return {"title": title, "see_all": "", "media_type": media_type, "media": []}

    items = list(_ordered(user, key, downloaded=custom_list)[:ROW_LIMIT])
    return {
        "title": title,
        "see_all": reverse("dashboard_row", args=[key]),
        "media_type": media_type,
        "media": _cards(user, key, items),
    }


def _downloaded_items(user, custom_list, media_type):
    """Return downloaded titles of this type, highest public score first."""
    if custom_list is None:
        return Item.objects.none()
    return _by_public_score(
        annotate_public_rating(
            Item.objects.filter(
                custom_lists=custom_list,
                media_type=media_type,
                season_number__isnull=True,
                episode_number__isnull=True,
            ),
            user,
            relation="public_rating",
            output="dashboard_score",
        ),
        "title",
        "dashboard_score",
    )


def _planning_row(user, media_type, title):
    """Tracked Planning titles, highest public score first."""
    key = _key("planning", media_type)
    media = list(_ordered(user, key)[:ROW_LIMIT])
    return {
        "title": title,
        "see_all": reverse("dashboard_row", args=[key]),
        "media_type": media_type,
        "media": _cards(user, key, media),
    }


def _planning_entries(user, media_type):
    """Return Planning entries of this type, highest public score first."""
    model = _model(media_type)
    return _by_public_score(
        annotate_public_rating(
            _latest(model.objects.filter(user=user, status=Status.PLANNING.value)),
            user,
        ),
        "item__title",
    ).select_related("item")


def _cards(user, key, records):
    """Turn an ordered slice into the cards both views render."""
    kind, media_type = _ROWS[key]
    if kind == "downloaded":
        return _attach_tracking(user, media_type, records)
    if kind == "planning" and media_type == MediaTypes.TV.value:
        _prefetch_episodes(records)
    elif kind == "continue":
        _label_continue(records)
    return records


def _attach_tracking(user, media_type, items):
    """Pair each list item with the user's tracking row, when they have one."""
    tracked = _tracked_for(user, media_type, [item.id for item in items])
    media = []
    for item in items:
        entry = tracked.get(item.id)
        if entry is None:
            entry = _ListedMedia(item, item.dashboard_score)
        else:
            entry.public_rating = item.dashboard_score
        media.append(entry)
    return media


def queue_refresh(user):
    """Queue one calendar reload and status re-check per minute."""
    from app.tasks import refresh_dashboard  # noqa: PLC0415

    key = f"dashboard_refresh_{user.pk}"
    if not cache.add(key, "pending", timeout=REFRESH_COOLDOWN):
        return None
    task = refresh_dashboard.delay(user.pk)
    cache.set(key, task.id, timeout=REFRESH_COOLDOWN)
    return task.id


def refresh_is_current(user, task_id):
    """Return whether this poll belongs to the user's latest refresh."""
    return cache.get(f"dashboard_refresh_{user.pk}") == task_id


def refresh_for_user(user):
    """Reload this user's calendar, then complete shows that have finished airing."""
    from events.tasks import reload_calendar  # noqa: PLC0415

    reload_calendar(user)
    return len(complete_finished_shows(user))


def complete_finished_shows(user):
    """Mark fully watched ended or canceled shows as completed."""
    shows = (
        TV.objects.filter(user=user, status=Status.IN_PROGRESS.value)
        .filter(Exists(_aired_events()))
        .exclude(Exists(_unwatched_aired()))
        .select_related("item")
    )
    finished = []
    for show in shows:
        if _tmdb_status(show) not in FINISHED_TMDB_STATUSES:
            continue
        show.status = Status.COMPLETED.value
        finished.append(show)
    if finished:
        bulk_update_with_history(finished, TV, ["status"], default_user=user)
    return finished


def _tmdb_status(show):
    """Return the cached TMDB show status, or nothing when it cannot be read."""
    if show.item.source != Sources.TMDB.value:
        return ""
    try:
        metadata = services.get_media_metadata(
            MediaTypes.TV.value,
            show.item.media_id,
            show.item.source,
        )
    except services.ProviderAPIError:
        logger.info("TMDB status unavailable for %s", show.item.media_id)
        return ""
    return (metadata.get("details") or {}).get("status") or ""


def _continue_row(user):
    """In-progress shows with an aired episode still to watch, newest watch first."""
    key = "continue-watching"
    shows = list(_ordered(user, key)[:ROW_LIMIT])
    return {
        "key": "continue",
        "title": _title(key),
        "see_all": reverse("dashboard_row", args=[key]),
        "media_type": MediaTypes.TV.value,
        "media": _cards(user, key, shows),
    }


def _continue_shows(user):
    """Return in-progress shows with an aired episode left, newest watch first."""
    queryset = (
        TV.objects.filter(user=user, status=Status.IN_PROGRESS.value)
        .filter(Exists(_unwatched_aired()))
        .select_related("item")
        .annotate(
            last_watched_at=Max(
                "seasons__episodes__end_date",
                filter=Q(seasons__item__season_number__gt=0),
            ),
        )
    )
    return annotate_public_rating(queryset, user).order_by(
        F("last_watched_at").desc(nulls_last=True),
        Lower("item__title"),
    )


def _label_continue(shows):
    """Mark each card with its next aired episode and how many remain."""
    if not shows:
        return
    pending = _pending_episodes(shows)
    for show in shows:
        episodes = pending[show.pk]
        if not episodes:
            continue
        season, number = min(episodes)
        show.next_episode = f"S{season}E{number}"
        show.episodes_remaining = len(episodes)


def _pending_episodes(shows):
    """Return aired, unwatched episodes for each show, including unstored seasons."""
    by_item = {}
    for show in shows:
        by_item.setdefault((show.item.media_id, show.item.source), []).append(show.pk)
    aired = Event.objects.filter(
        item__media_id__in={media_id for media_id, _source in by_item},
        item__source__in={source for _media_id, source in by_item},
        item__media_type=MediaTypes.SEASON.value,
        item__season_number__gt=0,
        content_number__isnull=False,
        datetime__lte=timezone.now(),
    ).values_list(
        "item__media_id",
        "item__source",
        "item__season_number",
        "content_number",
    )
    watched = set(
        Episode.objects.filter(
            related_season__related_tv_id__in=[show.pk for show in shows],
            related_season__item__season_number__gt=0,
            end_date__isnull=False,
        ).values_list(
            "related_season__related_tv_id",
            "related_season__item__season_number",
            "item__episode_number",
        ),
    )
    pending = {show.pk: set() for show in shows}
    for media_id, source, season, episode in aired:
        for show_id in by_item.get((media_id, source), ()):
            if (show_id, season, episode) not in watched:
                pending[show_id].add((season, episode))
    return pending


def _by_public_score(queryset, title_field, score_field="public_rating"):
    return queryset.order_by(
        F(score_field).desc(nulls_last=True),
        Lower(title_field),
    )


def _latest(queryset):
    """Keep the newest tracking row when a title was logged more than once."""
    return queryset.annotate(
        row_number=Window(
            expression=RowNumber(),
            partition_by=[F("item")],
            order_by=F("created_at").desc(),
        ),
    ).filter(row_number=1)


def _tracked_for(user, media_type, item_ids):
    if not item_ids:
        return {}
    queryset = (
        _model(media_type)
        .objects.filter(
            user=user,
            item_id__in=item_ids,
        )
        .select_related("item")
    )
    if media_type == MediaTypes.TV.value:
        queryset = queryset.prefetch_related(*_episode_prefetches())
    found = {}
    for entry in queryset.order_by("-created_at"):
        found.setdefault(entry.item_id, entry)
    return found


def _prefetch_episodes(shows):
    if shows:
        prefetch_related_objects(shows, *_episode_prefetches())


def _episode_prefetches():
    return (
        Prefetch("seasons", queryset=Season.objects.select_related("item")),
        Prefetch(
            "seasons__episodes",
            queryset=Episode.objects.select_related("item"),
        ),
    )


def _model(media_type):
    if media_type == MediaTypes.TV.value:
        return TV
    return Movie


def _aired_events():
    """Season events that have already aired, excluding specials."""
    return Event.objects.filter(
        item__media_id=OuterRef("item__media_id"),
        item__source=OuterRef("item__source"),
        item__media_type=MediaTypes.SEASON.value,
        item__season_number__gt=0,
        content_number__isnull=False,
        datetime__lte=timezone.now(),
    )


def _unwatched_aired():
    """Aired episodes this user has not watched."""
    watched = Episode.objects.filter(
        related_season__user_id=OuterRef(OuterRef("user_id")),
        related_season__item__media_id=OuterRef("item__media_id"),
        related_season__item__source=OuterRef("item__source"),
        related_season__item__season_number=OuterRef("item__season_number"),
        item__episode_number=OuterRef("content_number"),
        end_date__isnull=False,
    )
    return _aired_events().exclude(Exists(watched))
