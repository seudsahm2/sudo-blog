from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand

from blog.models import NewsSource, Post


class Command(BaseCommand):
    help = 'Report analytics retention policy and current cached click signal coverage.'

    def handle(self, *args, **options):
        retention_days = max(1, int(getattr(settings, 'ANALYTICS_RETENTION_DAYS', 30)))

        post_ids = list(Post.published.values_list('id', flat=True))
        source_ids = list(NewsSource.objects.filter(is_active=True).values_list('id', flat=True))

        post_last_seen_count = 0
        source_last_seen_count = 0
        for post_id in post_ids:
            if cache.get(f'analytics:clicks:post:{post_id}:last_seen'):
                post_last_seen_count += 1

        for source_id in source_ids:
            if cache.get(f'analytics:clicks:source:{source_id}:last_seen'):
                source_last_seen_count += 1

        self.stdout.write(f'ANALYTICS_RETENTION_DAYS={retention_days}')
        self.stdout.write(f'Published posts tracked: {len(post_ids)}')
        self.stdout.write(f'Active sources tracked: {len(source_ids)}')
        self.stdout.write(f'Post last_seen keys present: {post_last_seen_count}')
        self.stdout.write(f'Source last_seen keys present: {source_last_seen_count}')
