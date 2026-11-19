from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app.models import Book, Item, ItemRating, MediaTypes
from events.models import Event
from lists.models import CustomListItem


class Command(BaseCommand):
    """Delete book items that no user Book row points at."""

    help = (
        "Delete book items with no Book rows, plus their ratings, list "
        "memberships, and events. Does not delete other media types."
    )

    def add_arguments(self, parser):
        """Accept --dry-run to report without deleting."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be deleted.",
        )

    def handle(self, *_args, **options):
        """Delete untracked book items, or only print the counts."""
        book_type = MediaTypes.BOOK.value
        orphan_ids = list(
            Item.objects.filter(media_type=book_type)
            .exclude(pk__in=Book.objects.values("item_id"))
            .values_list("pk", flat=True),
        )
        if (
            Item.objects.filter(pk__in=orphan_ids)
            .exclude(media_type=book_type)
            .exists()
        ):
            message = "Refusing to delete non-book items."
            raise CommandError(message)

        ratings = ItemRating.objects.filter(item_id__in=orphan_ids)
        memberships = CustomListItem.objects.filter(item_id__in=orphan_ids)
        events = Event.objects.filter(item_id__in=orphan_ids)
        counts = (
            len(orphan_ids),
            ratings.count(),
            memberships.count(),
            events.count(),
        )
        if options["dry_run"]:
            self.stdout.write(_summary("Would delete", counts))
            return

        with transaction.atomic():
            ratings.delete()
            memberships.delete()
            events.delete()
            Item.objects.filter(pk__in=orphan_ids, media_type=book_type).delete()
        self.stdout.write(_summary("Deleted", counts))


def _summary(verb, counts):
    """Return the book, rating, list, and event counts."""
    items, ratings, memberships, events = counts
    return (
        f"{verb} {items} book items, {ratings} ratings, "
        f"{memberships} list items, {events} events."
    )
