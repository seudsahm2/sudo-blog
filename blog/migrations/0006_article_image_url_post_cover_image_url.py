from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0005_newslettersubscriber'),
    ]

    operations = [
        migrations.AddField(
            model_name='article',
            name='image_url',
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name='post',
            name='cover_image_url',
            field=models.URLField(blank=True),
        ),
    ]
