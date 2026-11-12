from django.conf import settings
from django.test import TestCase
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from django_celery_beat.schedulers import DatabaseScheduler

from config.celery import app as celery_app


class BeatScheduleSyncTests(TestCase):
    """DatabaseScheduler applies the fixed nightly crontabs."""

    def test_interval_row_becomes_the_public_ratings_crontab(self):
        """An existing interval for public ratings becomes 30 0 * * * only."""
        interval = IntervalSchedule.objects.create(every=86400, period="seconds")
        PeriodicTask.objects.create(
            name="refresh_public_ratings",
            task="Refresh public ratings",
            interval=interval,
        )
        other_interval = IntervalSchedule.objects.create(every=1, period="days")
        other = PeriodicTask.objects.create(
            name="Import from Trakt for user 1",
            task="Import from Trakt",
            interval=other_interval,
        )

        scheduler = DatabaseScheduler(celery_app, lazy=True)
        try:
            scheduler.setup_schedule()
        finally:
            scheduler._finalize.cancel()

        task = PeriodicTask.objects.get(name="refresh_public_ratings")
        self.assertIsNone(task.interval_id)
        self.assertIsNone(task.solar_id)
        self.assertIsNone(task.clocked_id)
        cron = task.crontab
        self.assertEqual(cron.minute, "30")
        self.assertEqual(cron.hour, "0")
        self.assertEqual(cron.day_of_month, "*")
        self.assertEqual(cron.month_of_year, "*")
        self.assertEqual(cron.day_of_week, "*")
        self.assertEqual(str(cron.timezone), settings.TIME_ZONE)

        other.refresh_from_db()
        self.assertEqual(other.interval_id, other_interval.id)
        self.assertIsNone(other.crontab_id)
