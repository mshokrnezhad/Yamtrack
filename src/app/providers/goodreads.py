from app.models import Item, ItemRating, MediaTypes, Sources
from app.providers import services

GOODREADS_SCORE_SCALE = 2


def book(media_id):
    """Return stored Goodreads metadata. Does not make HTTP requests."""
    item = (
        Item.objects.filter(
            media_id=str(media_id),
            source=Sources.GOODREADS.value,
            media_type=MediaTypes.BOOK.value,
        )
        .select_related("public_rating")
        .first()
    )
    if item is None:
        services.raise_not_found_error(
            Sources.GOODREADS.value,
            media_id,
            MediaTypes.BOOK.value,
        )

    rating = _rating(item)
    pages = rating.page_count if rating is not None else None
    return {
        "media_id": item.media_id,
        "source": Sources.GOODREADS.value,
        "source_url": f"https://www.goodreads.com/book/show/{item.media_id}",
        "media_type": MediaTypes.BOOK.value,
        "title": item.title,
        "max_progress": pages,
        "image": item.image,
        "synopsis": "No synopsis available.",
        "genres": [],
        "score": _score(rating),
        "score_count": rating.goodreads_ratings_count if rating is not None else None,
        "details": {
            "author": _text(rating, "author"),
            "number_of_pages": pages,
            "year": rating.publication_year if rating is not None else None,
            "isbn": _text(rating, "isbn"),
        },
        "related": {},
    }


def _rating(item):
    """Return the item rating, or None when this book has no rating row."""
    try:
        return item.public_rating
    except ItemRating.DoesNotExist:
        return None


def _text(rating, field):
    """Return a stored string, or None when the cell was blank."""
    if rating is None:
        return None
    return getattr(rating, field) or None


def _score(rating):
    """Scale a stored 1-5 Goodreads average onto the 0-10 detail score."""
    if rating is None or rating.goodreads_value is None:
        return None
    return round(float(rating.goodreads_value) * GOODREADS_SCORE_SCALE, 1)
