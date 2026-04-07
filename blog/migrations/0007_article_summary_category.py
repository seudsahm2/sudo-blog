from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0006_article_image_url_post_cover_image_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="article",
            name="summary_category",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]
