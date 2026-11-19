from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from integrations.imports import goodreads


class Command(BaseCommand):
    """Apply a Goodreads ratings CSV and optional reading progress."""

    help = (
        "Set Goodreads averages, rating counts, pages read, and cover URLs "
        "from CSVs. Does not call the network or mark books completed."
    )

    def add_arguments(self, parser):
        """Accept the ratings CSV, an optional progress CSV, and a user."""
        parser.add_argument(
            "ratings_csv",
            help="CSV with book_id, average, and ratings_count.",
        )
        parser.add_argument(
            "--progress",
            dest="progress_csv",
            default="",
            help="CSV with book_id,pages_read,total_pages.",
        )
        parser.add_argument(
            "--covers",
            dest="covers_csv",
            default="",
            help="CSV with book_id,cover_url. Does not download image files.",
        )
        parser.add_argument(
            "--username",
            default="",
            help="User whose currently-reading progress is updated.",
        )

    def handle(self, *_args, **options):
        """Apply the CSVs and print updated, missing, repaired, and created."""
        user = _user(options["username"])
        ratings_csv = options["ratings_csv"]
        progress_csv = options["progress_csv"] or None
        covers_csv = options["covers_csv"] or None
        _require_file(ratings_csv)
        if progress_csv:
            _require_file(progress_csv)
        if covers_csv:
            _require_file(covers_csv)
        stats = goodreads.apply_supplement(
            ratings_csv,
            user,
            progress_csv,
            covers_csv,
        )
        self.stdout.write(goodreads.format_summary(stats))


def _user(username):
    """Return the command's user, or raise CommandError."""
    try:
        return goodreads.resolve_user(username)
    except ValueError as error:
        raise CommandError(str(error)) from error


def _require_file(path):
    """Raise CommandError when the CSV path is missing."""
    if not Path(path).is_file():
        message = f"CSV not found: {path}"
        raise CommandError(message)
