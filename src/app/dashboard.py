import logging
from urllib.parse import urlencode

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
DOWNLOADED_LIST = "Downloaded"
REFRESH_COOLDOWN = 60
FINISHED_TMDB_STATUSES = frozenset({"Ended", "Canceled"})


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


def build_rows(user):
    """Return the dashboard rows that have something to show."""
    downloaded = (
        CustomList.objects.filter(
            owner=user,
            name=DOWNLOADED_LIST,
        )
        .order_by("id")
        .first()
    )
    rows = [
        _list_row(
            user,
            downloaded,
            MediaTypes.MOVIE.value,
            _("Top Downloaded movies"),
        ),
        _planning_row(user, MediaTypes.MOVIE.value, _("Top Planning movies")),
        _list_row(
            user,
            downloaded,
            MediaTypes.TV.value,
            _("Top Downloaded TV"),
        ),
        _planning_row(user, MediaTypes.TV.value, _("Top Planning TV")),
    ]
    rows = [row for row in rows if row["media"]]
    rows.append(_continue_row(user))
    return rows


def _list_row(user, custom_list, media_type, title):
    """Movies or shows in the Downloaded list, highest public score first."""
    if custom_list is None:
        return {"title": title, "see_all": "", "media_type": media_type, "media": []}

    items = _by_public_score(
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
    items = list(items[:ROW_LIMIT])
    tracked = _tracked_for(user, media_type, [item.id for item in items])
    media = []
    for item in items:
        entry = tracked.get(item.id)
        if entry is None:
            entry = _ListedMedia(item, item.dashboard_score)
        else:
            entry.public_rating = item.dashboard_score
        media.append(entry)
    return {
        "title": title,
        "see_all": f"{reverse('list_detail', args=[custom_list.id])}?type={media_type}",
        "media_type": media_type,
        "media": media,
    }


def _planning_row(user, media_type, title):
    """Tracked Planning titles, highest public score first."""
    model = _model(media_type)
    queryset = _by_public_score(
        annotate_public_rating(
            _latest(model.objects.filter(user=user, status=Status.PLANNING.value)),
            user,
        ),
        "item__title",
    ).select_related("item")
    media = list(queryset[:ROW_LIMIT])
    if media_type == MediaTypes.TV.value:
        _prefetch_episodes(media)
    return {
        "title": title,
        "see_all": _media_list_url(
            user,
            media_type,
            {"status": Status.PLANNING.value, "sort": "public_rating"},
        ),
        "media_type": media_type,
        "media": media,
    }


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
    shows = list(
        annotate_public_rating(queryset, user).order_by(
            F("last_watched_at").desc(nulls_last=True),
            Lower("item__title"),
        )[:ROW_LIMIT],
    )
    _label_continue(shows)
    return {
        "key": "continue",
        "title": _("Continue watching"),
        "see_all": _media_list_url(
            user,
            MediaTypes.TV.value,
            {"status": Status.IN_PROGRESS.value},
        ),
        "media_type": MediaTypes.TV.value,
        "media": shows,
    }


def _label_continue(shows):
    """Mark each card with its next aired episode and how many remain."""
    if not shows:
        return
    now = timezone.now()
    prefetch_related_objects(shows, _continue_prefetch(now))
    for show in shows:
        pending = []
        for season in show.seasons.all():
            number = season.item.season_number
            if not number:
                continue
            watched = {
                episode.item.episode_number
                for episode in season.episodes.all()
                if episode.end_date is not None
            }
            for event in season.item.event_set.all():
                if event.datetime > now or event.content_number in watched:
                    continue
                pending.append((number, event.content_number))
        if not pending:
            continue
        pending.sort()
        show.next_episode = f"S{pending[0][0]}E{pending[0][1]}"
        show.episodes_remaining = len(pending)


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


def _media_list_url(user, media_type, query):
    base = reverse("medialist", args=[user.username, media_type])
    return f"{base}?{urlencode(query)}"


def _continue_prefetch(now):
    aired = Event.objects.filter(content_number__isnull=False, datetime__lte=now)
    return Prefetch(
        "seasons",
        queryset=Season.objects.select_related("item").prefetch_related(
            Prefetch("episodes", queryset=Episode.objects.select_related("item")),
            Prefetch("item__event_set", queryset=aired),
        ),
    )


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
