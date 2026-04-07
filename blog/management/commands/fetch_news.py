from django.core.management.base import BaseCommand
from django.conf import settings

from blog.models import NewsSource
from blog.services import NewsIngestionService


class Command(BaseCommand):
    help = "Fetch and ingest news from active configured sources"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-id",
            type=int,
            help="Only fetch from one NewsSource id",
        )
        parser.add_argument(
            "--max-items",
            type=int,
            default=20,
            help="Maximum records to request from each provider",
        )

    def handle(self, *args, **options):
        source_id = options.get("source_id")
        max_items = options.get("max_items")

        queryset = NewsSource.objects.filter(is_active=True)
        if source_id:
            queryset = queryset.filter(id=source_id)

        sources = list(queryset)
        if not sources:
            self.stdout.write(self.style.WARNING("No active sources found."))
            return

        service = NewsIngestionService()
        for source in sources:
            if source.provider == NewsSource.Provider.TELEGRAM and not getattr(settings, 'FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED', False):
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping source {source.name}: Telegram ingestion feature flag disabled."
                    )
                )
                continue

            self.stdout.write(self.style.NOTICE(f"Fetching source: {source.name}"))
            try:
                result = service.fetch_and_store(source=source, max_items=max_items)
            except Exception as exc:
                self.stderr.write(
                    self.style.ERROR(f"Failed source {source.name}: {exc}")
                )
                continue

            self.stdout.write(
                self.style.SUCCESS(
                    f"{result.source_name}: fetched={result.fetched}, created={result.created}, updated={result.updated}"
                )
            )
