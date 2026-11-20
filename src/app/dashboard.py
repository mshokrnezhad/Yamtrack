import logging
from decimal import ROUND_HALF_UP, Decimal

from django.core.cache import cache
from django.db.models import (
    Exists,
    F,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Window,
    prefetch_related_objects,
)
from django.db.models.functions import Lower, RowNumber
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from simple_history.utils import bulk_update_with_history

from app.models import (
    TV,
    Book,
    Episode,
    Game,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.providers import services
from app.ratings import annotate_public_rating
from events.models import Event
from lists.models import CustomList

logger = logging.getLogger(__name__)

ROW_LIMIT = 10
PAGE_SIZE = 32
DOWNLOADED_LIST = "Downloaded"
XBOX_LIST = "Xbox"
SWITCH_LIST = "Nintendo Switch"
REFRESH_COOLDOWN = 60
FINISHED_TMDB_STATUSES = frozenset({"Ended", "Canceled"})
_UNSET = object()
_PLAYING = (Status.IN_PROGRESS.value, Status.PAUSED.value)
_UNREAD = (
    Status.IN_PROGRESS.value,
    Status.PAUSED.value,
    Status.PLANNING.value,
    Status.DROPPED.value,
)
_NOT_STARTED = (Status.PLANNING.value,)
_ROWS = {
    "top-downloaded-movies": ("downloaded", MediaTypes.MOVIE.value),
    "top-planning-movies": ("planning", MediaTypes.MOVIE.value),
    "top-downloaded-tv": ("downloaded", MediaTypes.TV.value),
    "top-planning-tv": ("planning", MediaTypes.TV.value),
    "continue-watching": ("continue", MediaTypes.TV.value),
    "new-episodes": ("new", MediaTypes.TV.value),
}
_GAME_ROWS = {
    "continue-playing-xbox": ("continue", XBOX_LIST),
    "continue-playing-switch": ("continue", SWITCH_LIST),
    "best-owned-xbox": ("best", XBOX_LIST),
    "best-owned-switch": ("best", SWITCH_LIST),
}
CURRENTLY_READING_KEY = "currently-reading"
BOOK_TAG_PREFIX = "book-tag-"
# Exclusive Goodreads shelves are a status. Keep in step with the importer.
EXCLUSIVE_SHELVES = frozenset(
    {
        "read",
        "currently-reading",
        "to-read",
        "did-not-finish",
    },
)
_EMPTY = {
    "continue-watching": _("No shows in progress."),
    "new-episodes": _("No new episodes."),
    "continue-playing-xbox": _("No games in progress."),
    "continue-playing-switch": _("No games in progress."),
    "best-owned-xbox": _("No unstarted owned games."),
    "best-owned-switch": _("No unstarted owned games."),
    CURRENTLY_READING_KEY: _("No books in progress."),
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
        "new-episodes": _("New episodes"),
        "continue-playing-xbox": _("Continue Playing - Xbox"),
        "continue-playing-switch": _("Continue Playing - Switch"),
        "best-owned-xbox": _("Best Owned - Xbox"),
        "best-owned-switch": _("Best Owned - Switch"),
    }
    return titles[key]


_SIDEBAR_LABELS = {
    "top-downloaded-movies": _("Top Downloaded"),
    "top-planning-movies": _("Top Planning"),
    "top-downloaded-tv": _("Top Downloaded"),
    "top-planning-tv": _("Top Planning"),
    "continue-watching": _("Continue watching"),
    "new-episodes": _("New episodes"),
    "continue-playing-xbox": _("Continue Playing - Xbox"),
    "continue-playing-switch": _("Continue Playing - Switch"),
    "best-owned-xbox": _("Best Owned - Xbox"),
    "best-owned-switch": _("Best Owned - Switch"),
    CURRENTLY_READING_KEY: _("Currently reading"),
}
_SIDEBAR_GROUPS = (
    (
        _("Movies"),
        MediaTypes.MOVIE.value,
        "dashboard_movies",
        ("top-downloaded-movies", "top-planning-movies"),
    ),
    (
        _("Series"),
        MediaTypes.TV.value,
        "dashboard_series",
        (
            "top-downloaded-tv",
            "top-planning-tv",
            "continue-watching",
            "new-episodes",
        ),
    ),
    (_("Games"), MediaTypes.GAME.value, "dashboard_games", tuple(_GAME_ROWS)),
    (_("Books"), MediaTypes.BOOK.value, "dashboard_books", (CURRENTLY_READING_KEY,)),
)


MARQUEE_LIMIT = 12


def marquee_items(user):
    """Return the items in progress across categories, most recent first."""
    in_progress = []
    for model in (TV, Movie, Game, Book):
        queryset = model.objects.filter(
            user=user,
            status=Status.IN_PROGRESS.value,
        ).select_related("item")
        # TV has no progress of its own; its seasons and episodes carry it.
        if model is TV:
            queryset = queryset.order_by("-created_at")
        else:
            queryset = queryset.order_by(
                F("progressed_at").desc(nulls_last=True),
                "-created_at",
            )
        in_progress.extend(queryset[:MARQUEE_LIMIT])
    in_progress.sort(
        key=lambda media: getattr(media, "progressed_at", None) or media.created_at,
        reverse=True,
    )
    return [media.item for media in in_progress[:MARQUEE_LIMIT]]


def sidebar_sections(user):
    """Return one sidebar group per category, each linking its dashboard rows."""
    sections = []
    enabled = set(user.get_enabled_media_types())
    for title, media_type, dashboard_name, keys in _SIDEBAR_GROUPS:
        if media_type not in enabled:
            continue
        rows = [
            {
                "title": _SIDEBAR_LABELS[key],
                "url": reverse("dashboard_row", args=[key]),
            }
            for key in keys
        ]
        if media_type == MediaTypes.BOOK.value:
            rows.extend(
                {
                    "title": custom_list.name,
                    "url": reverse(
                        "dashboard_row",
                        args=[f"{BOOK_TAG_PREFIX}{custom_list.id}"],
                    ),
                }
                for custom_list in _book_tag_lists(user)
            )
        sections.append(
            {
                "title": title,
                "media_type": media_type,
                "url": reverse(dashboard_name),
                "library_url": reverse(
                    "medialist",
                    kwargs={"username": user.username, "media_type": media_type},
                ),
                "rows": rows,
            },
        )
    return sections


def _key(kind, media_type):
    """Return the route key for a row kind and media type."""
    for key, row in _ROWS.items():
        if row == (kind, media_type):
            return key
    message = "Unknown dashboard row"
    raise KeyError(message)


def build_movie_rows(user):
    """Return the movie rows that have something to show."""
    downloaded = _downloaded_list(user)
    rows = [
        _list_row(
            user,
            downloaded,
            MediaTypes.MOVIE.value,
            _title("top-downloaded-movies"),
        ),
        _planning_row(user, MediaTypes.MOVIE.value, _title("top-planning-movies")),
    ]
    return [row for row in rows if row["media"]]


def build_series_rows(user):
    """Return the series rows, always including the episode rows."""
    downloaded = _downloaded_list(user)
    rows = [
        _list_row(user, downloaded, MediaTypes.TV.value, _title("top-downloaded-tv")),
        _planning_row(user, MediaTypes.TV.value, _title("top-planning-tv")),
    ]
    rows = [row for row in rows if row["media"]]
    rows.append(_continue_row(user))
    rows.append(_new_episodes_row(user))
    return rows


def build_game_rows(user):
    """Return the four game rows, including ones with nothing to show."""
    lists = {
        XBOX_LIST: _named_list(user, XBOX_LIST),
        SWITCH_LIST: _named_list(user, SWITCH_LIST),
    }
    return [
        _game_row(user, key, lists[name]) for key, (_kind, name) in _GAME_ROWS.items()
    ]


def build_book_rows(user):
    """Return Currently reading, then one row per custom book tag."""
    rows = [_currently_reading_row(user)]
    rows.extend(
        _book_tag_row(user, custom_list) for custom_list in _book_tag_lists(user)
    )
    return rows


def row_page(user, key, page):
    """Return one page of the same ordered items as that dashboard row."""
    if key not in _ROWS and key not in _GAME_ROWS and not _book_row_exists(user, key):
        return None
    records, has_next, number = _slice_page(_ordered(user, key), page)
    return {
        "key": key,
        "title": _row_title(user, key),
        "media_type": _row_media_type(key),
        "entries": _cards(user, key, records),
        "has_next": has_next,
        "next_page": number + 1,
        "back_url": reverse(_row_dashboard(key)),
        "empty": _EMPTY.get(key),
    }


def _ordered(user, key, *, downloaded=_UNSET):
    """Return the ordered queryset for a row, with no length limit.

    The dashboard row and its See all page both call this.
    """
    if _is_book_key(key):
        return _ordered_books(user, key)
    if key in _GAME_ROWS:
        return _ordered_games(user, key)
    kind, media_type = _ROWS[key]
    if kind == "downloaded":
        if downloaded is _UNSET:
            downloaded = _downloaded_list(user)
        return _downloaded_items(user, downloaded, media_type)
    if kind == "planning":
        return _planning_entries(user, media_type)
    if kind == "new":
        return _new_episode_shows(user)
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


def _named_list(user, name):
    """Return the user's first list with this exact name."""
    return CustomList.objects.filter(owner=user, name=name).order_by("id").first()


def _downloaded_list(user):
    """Return the user's Downloaded list, if they have one."""
    return _named_list(user, DOWNLOADED_LIST)


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
    if key == CURRENTLY_READING_KEY:
        return _reading_cards(records)
    if key.startswith(BOOK_TAG_PREFIX):
        return _tag_cards(user, records)
    if key in _GAME_ROWS:
        return _game_cards(user, key, records)
    kind, media_type = _ROWS[key]
    if kind == "downloaded":
        return _attach_tracking(user, media_type, records)
    if kind == "planning" and media_type == MediaTypes.TV.value:
        _prefetch_episodes(records)
    elif kind in {"continue", "new"}:
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


def _episode_row(user, key):
    """One capped TV row. Continue watching and New episodes share this shape."""
    shows = list(_ordered(user, key)[:ROW_LIMIT])
    return {
        "key": _ROWS[key][0],
        "title": _title(key),
        "see_all": reverse("dashboard_row", args=[key]),
        "media_type": MediaTypes.TV.value,
        "media": _cards(user, key, shows),
        "empty": _EMPTY[key],
    }


def _continue_row(user):
    """In-progress shows with an aired episode still to watch, newest watch first."""
    return _episode_row(user, "continue-watching")


def continue_watching(user):
    """Return the Continue watching shows, each labeled with its next episode."""
    return _continue_row(user)["media"]


def currently_reading(user):
    """Return the in-progress books, each with its page count as max_progress."""
    return _reading_cards(list(_currently_reading(user)[:ROW_LIMIT]))


def _new_episodes_row(user):
    """In-progress shows with an aired episode still to watch, newest air date first."""
    return _episode_row(user, "new-episodes")


def _in_progress_unwatched(user):
    """In-progress shows with an aired episode still to watch."""
    return (
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


def _continue_shows(user):
    """Return in-progress shows with an aired episode left, newest watch first."""
    return annotate_public_rating(_in_progress_unwatched(user), user).order_by(
        F("last_watched_at").desc(nulls_last=True),
        Lower("item__title"),
    )


def _new_episode_shows(user):
    """Return those shows, most recently aired episode first."""
    queryset = _in_progress_unwatched(user).annotate(
        latest_aired_at=Subquery(
            _aired_events().order_by("-datetime").values("datetime")[:1],
        ),
    )
    return annotate_public_rating(queryset, user).order_by(
        F("latest_aired_at").desc(nulls_last=True),
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


def _game_row(user, key, game_list):
    """One capped game row. Continue Playing and Best Owned share this shape."""
    records = list(_ordered_games(user, key, game_list=game_list)[:ROW_LIMIT])
    return {
        "title": _title(key),
        "see_all": reverse("dashboard_row", args=[key]),
        "media_type": MediaTypes.GAME.value,
        "media": _game_cards(user, key, records),
        "empty": _EMPTY[key],
    }


def _ordered_games(user, key, *, game_list=_UNSET):
    """Return every game for this row, in the order the cards use."""
    kind, name = _GAME_ROWS[key]
    if game_list is _UNSET:
        game_list = _named_list(user, name)
    if kind == "continue":
        return _continue_playing(user, game_list)
    return _best_owned(user, game_list)


def _continue_playing(user, game_list):
    """In-progress or paused games on this list, newest history first."""
    if game_list is None:
        return Game.objects.none()
    activity = Subquery(
        Game.history.model.objects.filter(id=OuterRef("pk"))
        .order_by("-history_date")
        .values("history_date")[:1],
    )
    return (
        _latest(Game.objects.filter(user=user, item__custom_lists=game_list))
        .filter(status__in=_PLAYING)
        .annotate(
            card_score=F("item__public_rating__metacritic_value"),
            latest_activity=activity,
        )
        .select_related("item")
        .order_by(
            F("latest_activity").desc(nulls_last=True),
            Lower("item__title"),
        )
    )


def _best_owned(user, game_list):
    """List games not started yet, highest critic score first.

    Included when the user has no Game row, or the newest row is Planning.
    """
    if game_list is None:
        return Item.objects.none()
    latest_status = Subquery(
        Game.objects.filter(user=user, item_id=OuterRef("pk"))
        .order_by("-created_at")
        .values("status")[:1],
    )
    return (
        Item.objects.filter(
            custom_lists=game_list,
            media_type=MediaTypes.GAME.value,
        )
        .annotate(
            latest_status=latest_status,
            card_score=F("public_rating__metacritic_value"),
        )
        .filter(Q(latest_status__isnull=True) | Q(latest_status__in=_NOT_STARTED))
        .order_by(F("card_score").desc(nulls_last=True), Lower("title"))
    )


def _game_cards(user, key, records):
    """Attach tracking and the rounded critic score the badge shows."""
    if not records:
        return []
    kind, _name = _GAME_ROWS[key]
    if kind == "best":
        return _attach_game_tracking(user, records)
    for entry in records:
        entry.public_rating = _whole_score(entry.card_score)
    return records


def _attach_game_tracking(user, items):
    """Pair each list item with the user's newest game row, when they have one."""
    tracked = {}
    item_ids = [item.id for item in items]
    if item_ids:
        queryset = _latest(
            Game.objects.filter(user=user, item_id__in=item_ids),
        ).select_related("item")
        tracked = {entry.item_id: entry for entry in queryset}
    media = []
    for item in items:
        score = _whole_score(item.card_score)
        entry = tracked.get(item.id)
        if entry is None:
            entry = _ListedMedia(item, score)
        else:
            entry.public_rating = score
        media.append(entry)
    return media


def _whole_score(value):
    """Return a critic score as a whole number. Missing stays missing."""
    if value is None:
        return None
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _row_media_type(key):
    """Return the media type a See all page renders."""
    if key == CURRENTLY_READING_KEY or key.startswith(BOOK_TAG_PREFIX):
        return MediaTypes.BOOK.value
    if key in _GAME_ROWS:
        return MediaTypes.GAME.value
    return _ROWS[key][1]


def _row_dashboard(key):
    """Return the dashboard a See all page links back to."""
    if key == CURRENTLY_READING_KEY or key.startswith(BOOK_TAG_PREFIX):
        return "dashboard_books"
    if key in _GAME_ROWS:
        return "dashboard_games"
    if _ROWS[key][1] == MediaTypes.MOVIE.value:
        return "dashboard_movies"
    return "dashboard_series"


def _row_title(user, key):
    """Return the heading for a dashboard row."""
    if key == CURRENTLY_READING_KEY:
        return _("Currently reading")
    if key.startswith(BOOK_TAG_PREFIX):
        custom_list = _tag_list(user, key)
        if custom_list is None:
            return ""
        return custom_list.name
    return _title(key)


def _book_row_exists(user, key):
    """Return whether this key is a book row for the user."""
    if key == CURRENTLY_READING_KEY:
        return True
    return _tag_list(user, key) is not None


def _is_book_key(key):
    """Return whether this route key belongs to the books dashboard."""
    return key == CURRENTLY_READING_KEY or key.startswith(BOOK_TAG_PREFIX)


def _ordered_books(user, key):
    """Return the ordered books for a row. See all uses the same queryset."""
    if key == CURRENTLY_READING_KEY:
        return _currently_reading(user)
    return _tag_books(user, _tag_list(user, key))


def _book_tag_lists(user):
    """Return book lists that are not exclusive Goodreads shelves."""
    return (
        CustomList.objects.filter(
            owner=user,
            items__media_type=MediaTypes.BOOK.value,
        )
        .exclude(name__in=EXCLUSIVE_SHELVES)
        .distinct()
        .order_by("name", "id")
    )


def _currently_reading_row(user):
    """In-progress books, most recently progressed first."""
    books = list(_currently_reading(user)[:ROW_LIMIT])
    return {
        "title": _("Currently reading"),
        "see_all": reverse("dashboard_row", args=[CURRENTLY_READING_KEY]),
        "media_type": MediaTypes.BOOK.value,
        "media": _reading_cards(books),
        "empty": _EMPTY[CURRENTLY_READING_KEY],
    }


def _book_tag_row(user, custom_list):
    """Unread books on one custom shelf, highest Goodreads average first."""
    key = f"{BOOK_TAG_PREFIX}{custom_list.id}"
    items = list(_tag_books(user, custom_list)[:ROW_LIMIT])
    return {
        "title": custom_list.name,
        "see_all": reverse("dashboard_row", args=[key]),
        "media_type": MediaTypes.BOOK.value,
        "media": _tag_cards(user, items),
    }


def _currently_reading(user):
    """In-progress books, newest progress, then history, then title.

    Not sorted by rating. ``progressed_at`` is when the page count last changed.
    """
    activity = Subquery(
        Book.history.model.objects.filter(id=OuterRef("pk"))
        .order_by("-history_date")
        .values("history_date")[:1],
    )
    return (
        _latest(Book.objects.filter(user=user))
        .filter(status=Status.IN_PROGRESS.value)
        .annotate(
            latest_activity=activity,
            stored_pages=F("item__public_rating__page_count"),
        )
        .select_related("item")
        .order_by(
            F("progressed_at").desc(nulls_last=True),
            F("latest_activity").desc(nulls_last=True),
            Lower("item__title"),
        )
    )


def _reading_cards(books):
    """Show pages read against the imported page count."""
    for book in books:
        book.max_progress = book.stored_pages
    return books


def _tag_books(user, custom_list):
    """Books on this list that are not completed, highest average first."""
    if custom_list is None:
        return Item.objects.none()
    latest_status = Subquery(
        Book.objects.filter(user=user, item_id=OuterRef("pk"))
        .order_by("-created_at")
        .values("status")[:1],
    )
    return (
        Item.objects.filter(
            custom_lists=custom_list,
            media_type=MediaTypes.BOOK.value,
        )
        .annotate(
            latest_status=latest_status,
            goodreads_average=F("public_rating__goodreads_value"),
        )
        .filter(Q(latest_status__isnull=True) | Q(latest_status__in=_UNREAD))
        .order_by(F("goodreads_average").desc(nulls_last=True), Lower("title"))
    )


def _tag_cards(user, items):
    """Pair each book with its newest tracking row and the Goodreads average."""
    if not items:
        return []
    tracked = {}
    item_ids = [item.id for item in items]
    if item_ids:
        queryset = _latest(
            Book.objects.filter(user=user, item_id__in=item_ids),
        ).select_related("item")
        tracked = {entry.item_id: entry for entry in queryset}
    media = []
    for item in items:
        # The Item relation is already named public_rating, so the annotation
        # uses another name and the card attribute is set here.
        score = item.goodreads_average
        entry = tracked.get(item.id)
        if entry is None:
            entry = _ListedMedia(item, score)
        else:
            entry.public_rating = score
        media.append(entry)
    return media


def _tag_list(user, key):
    """Return this user's book tag list for a row key, if it is one."""
    list_id = _tag_list_id(key)
    if list_id is None:
        return None
    custom_list = (
        CustomList.objects.filter(pk=list_id, owner=user)
        .exclude(name__in=EXCLUSIVE_SHELVES)
        .first()
    )
    if custom_list is None:
        return None
    if not custom_list.items.filter(media_type=MediaTypes.BOOK.value).exists():
        return None
    return custom_list


def _tag_list_id(key):
    """Return the custom list id encoded in a book tag row key."""
    if not key.startswith(BOOK_TAG_PREFIX):
        return None
    raw = key.removeprefix(BOOK_TAG_PREFIX)
    if not raw.isdecimal():
        return None
    return int(raw)


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
