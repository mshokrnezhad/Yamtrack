from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from integrations.imports import goodreads
from integrations.imports.goodreads import GoodReadsImporter


class Command(BaseCommand):
    """Import missing Goodreads rows and repair mismatched titles."""

    help = (
        "Re-run a Goodreads export for one user. Rows with no Goodreads id "
        "are imported, and a title mismatch is moved off the wrong catalog item."
    )

    def add_arguments(self, parser):
        """Accept the export CSV, a user, and the import mode."""
        parser.add_argument("csv", help="Goodreads library export CSV.")
        parser.add_argument(
            "--username",
            default="",
            help="User to import and repair. Defaults to the earliest user.",
        )
        parser.add_argument(
            "--mode",
            default="new",
            choices=["new", "overwrite"],
            help="Import mode. Defaults to new.",
        )

    def handle(self, *_args, **options):
        """Import the export and print updated, missing, repaired, and created."""
        path = options["csv"]
        if not Path(path).is_file():
            message = f"CSV not found: {path}"
            raise CommandError(message)
        try:
            user = goodreads.resolve_user(options["username"])
        except ValueError as error:
            raise CommandError(str(error)) from error

        with Path(path).open("rb") as file:
            csv_importer = GoodReadsImporter(file, user, options["mode"])
            csv_importer.import_data()
        self.stdout.write(goodreads.format_summary(csv_importer.stats))
