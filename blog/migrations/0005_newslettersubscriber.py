from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0004_bookmark'),
    ]

    operations = [
        migrations.CreateModel(
            name='NewsletterSubscriber',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254, unique=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('last_sent_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['-created'],
            },
        ),
    ]
