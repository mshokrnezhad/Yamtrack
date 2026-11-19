from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0069_goodreads_item_rating'),
    ]

    operations = [
        migrations.AddField(
            model_name='itemrating',
            name='goodreads_ratings_count',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
