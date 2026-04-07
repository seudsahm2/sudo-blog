from django.core.management.base import BaseCommand

from blog.models import NewsSource


DEFAULT_SOURCES = [
    {
        "name": "Global Headlines (NewsAPI)",
        "provider": NewsSource.Provider.NEWSAPI,
        "base_url": "https://newsapi.org/v2/top-headlines",
        "trust_score": 80,
    },
    {
        "name": "Fast Coverage (GNews)",
        "provider": NewsSource.Provider.GNEWS,
        "base_url": "https://gnews.io/api/v4/top-headlines",
        "trust_score": 76,
    },
    {
        "name": "Breaking Wire (NewsData)",
        "provider": NewsSource.Provider.NEWSDATA,
        "base_url": "https://newsdata.io/api/1/news",
        "trust_score": 74,
    },
    {
        "name": "World Depth (The Guardian)",
        "provider": NewsSource.Provider.GUARDIAN,
        "base_url": "https://content.guardianapis.com/search",
        "trust_score": 85,
    },
    {
        "name": "Space and Science (Spaceflight)",
        "provider": NewsSource.Provider.SPACEFLIGHT,
        "base_url": "https://api.spaceflightnewsapi.net/v4/articles/",
        "trust_score": 72,
    },
    {
        "name": "Sports Pulse (OpenLigaDB)",
        "provider": NewsSource.Provider.OPENLIGADB,
        "base_url": "https://api.openligadb.de/getmatchdata/bl1",
        "trust_score": 70,
    },
]


class Command(BaseCommand):
    help = "Create or update a diverse set of news and sports sources for richer editorial coverage."

    def handle(self, *args, **options):
        created = 0
        updated = 0

        for item in DEFAULT_SOURCES:
            obj, is_created = NewsSource.objects.update_or_create(
                name=item["name"],
                defaults={
                    "provider": item["provider"],
                    "base_url": item["base_url"],
                    "is_active": True,
                    "auto_publish": True,
                    "trust_score": item["trust_score"],
                    "fetch_interval_minutes": 60,
                    "notes": "Seeded for broad coverage: world, tech/space, and sports.",
                },
            )
            if is_created:
                created += 1
            else:
                updated += 1

            self.stdout.write(f"- {obj.name} [{obj.provider}] active={obj.is_active} auto_publish={obj.auto_publish}")

        self.stdout.write(self.style.SUCCESS(f"Diverse sources ready: created={created}, updated={updated}"))
