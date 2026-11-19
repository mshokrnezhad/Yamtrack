from django.core.management.base import BaseCommand

from app.ratings import refresh_game_critic_scores


class Command(BaseCommand):
    """Backfill IGDB critic scores for games already in the library."""

    help = (
        "Store IGDB aggregated critic scores on games. "
        "By default only missing or stale scores are refreshed."
    )

    def add_arguments(self, parser):
        """Accept --all to refresh every game."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="Refresh every game, including scores that are still fresh.",
        )

    def handle(self, *_args, **options):
        """Refresh critic scores and report how many games were saved."""
        saved = refresh_game_critic_scores(stale_only=not options["all"])
        self.stdout.write(f"Updated {saved} games.")
