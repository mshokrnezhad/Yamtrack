from urllib.parse import urlencode

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

from app.models import TV, Episode, Item, MediaTypes, Movie, Season, Status
from app.ratings import annotate_public_rating
from events.models import Event
from lists.models import CustomList

ROW_LIMIT = 10
DOWNLOADED_LIST = "Downloaded"


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
        _caught_up_row(user),
    ]
    return [row for row in rows if row["media"]]


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


def _caught_up_row(user):
    """In-progress shows with every aired episode watched, newest watch first."""
    shows = list(
        TV.objects.filter(user=user, status=Status.IN_PROGRESS.value)
        .filter(Exists(_aired_events()))
        .exclude(Exists(_unwatched_aired()))
        .select_related("item")
        .annotate(
            last_watched_at=Max(
                "seasons__episodes__end_date",
                filter=Q(seasons__item__season_number__gt=0),
            ),
        )
        .order_by(F("last_watched_at").desc(nulls_last=True), Lower("item__title"))[
            :ROW_LIMIT
        ],
    )
    _prefetch_episodes(shows)
    return {
        "title": _("Caught up"),
        "see_all": _media_list_url(
            user,
            MediaTypes.TV.value,
            {"status": Status.IN_PROGRESS.value},
        ),
        "media_type": MediaTypes.TV.value,
        "media": shows,
    }


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
