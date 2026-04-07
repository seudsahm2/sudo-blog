import re
import html
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.management.base import BaseCommand

from blog.models import Article, Post


class Command(BaseCommand):
    help = "Repair ingested article/post text artifacts and backfill image URLs from page metadata."

    def add_arguments(self, parser):
        parser.add_argument("--skip-image-fetch", action="store_true")
        parser.add_argument("--skip-fulltext-fetch", action="store_true")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **options):
        skip_image_fetch = options["skip_image_fetch"]
        skip_fulltext_fetch = options["skip_fulltext_fetch"]
        limit = max(0, int(options["limit"] or 0))

        truncation_pattern = re.compile(r"\s*\[\+\d+\s+chars\]\s*$")
        cleaned_articles = 0
        fetched_images = 0
        fetched_fulltext = 0

        articles_qs = Article.objects.all().order_by("-fetched_at")
        if limit:
            articles_qs = articles_qs[:limit]

        for article in articles_qs:
            body = (article.body or "").strip()
            summary = (article.summary or "").strip()
            new_body = truncation_pattern.sub("", body).strip()
            new_summary = truncation_pattern.sub("", summary).strip()

            changed = False
            if new_body != body:
                article.body = new_body
                changed = True
            if new_summary != summary:
                article.summary = new_summary
                changed = True

            if not skip_fulltext_fetch:
                min_words_for_full = max(1, int(getattr(settings, "FULL_ARTICLE_MIN_WORDS", 140)))
                needs_fulltext = len((article.body or "").split()) < min_words_for_full
                if needs_fulltext:
                    full_text = self._fetch_full_text(article.source_url)
                    if len(full_text.split()) > len((article.body or "").split()):
                        article.body = full_text
                        changed = True
                        fetched_fulltext += 1

            if not skip_image_fetch and not article.image_url:
                image_url = self._fetch_og_image(article.source_url)
                if image_url:
                    article.image_url = image_url
                    fetched_images += 1
                    changed = True

            if changed:
                article.save(update_fields=["body", "summary", "image_url", "updated"])
                cleaned_articles += 1

        synced_posts = 0
        posts_qs = Post.objects.select_related("source_article").filter(
            auto_generated=True,
            source_article__isnull=False,
        )
        for post in posts_qs:
            article = post.source_article
            if article is None:
                continue
            changed = False
            target_body = (article.summary or article.body or "").strip()
            if post.body != target_body:
                post.body = target_body
                changed = True
            if post.summary != article.summary:
                post.summary = article.summary
                changed = True
            if (not post.cover_image_url) and article.image_url:
                post.cover_image_url = article.image_url
                changed = True

            if changed:
                post.save(update_fields=["body", "summary", "cover_image_url", "updated"])
                synced_posts += 1

        self.stdout.write(f"Cleaned articles: {cleaned_articles}")
        self.stdout.write(f"Fetched image URLs: {fetched_images}")
        self.stdout.write(f"Fetched full text bodies: {fetched_fulltext}")
        self.stdout.write(f"Synced posts: {synced_posts}")
        self.stdout.write(f"Articles with image_url: {Article.objects.exclude(image_url='').count()}")
        self.stdout.write(f"Posts with cover_image_url: {Post.objects.exclude(cover_image_url='').count()}")

    def _fetch_og_image(self, source_url: str) -> str:
        allow_insecure_ssl = bool(getattr(settings, "ALLOW_INSECURE_SSL_FETCH", True))
        try:
            request = Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Connection": "close",
                },
            )
            with urlopen(request, timeout=6) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/html" not in content_type:
                    return ""
                html_content = response.read(350000).decode("utf-8", errors="ignore")
        except ssl.SSLError:
            if not allow_insecure_ssl:
                return ""
            try:
                insecure_context = ssl._create_unverified_context()  # type: ignore[attr-defined]
                with urlopen(request, timeout=6, context=insecure_context) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if "text/html" not in content_type:
                        return ""
                    html_content = response.read(350000).decode("utf-8", errors="ignore")
            except (HTTPError, URLError, TimeoutError, ValueError, ssl.SSLError):
                return ""
        except (HTTPError, URLError, TimeoutError, ValueError):
            return ""

        match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html_content,
            re.IGNORECASE,
        ) or re.search(
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            html_content,
            re.IGNORECASE,
        )

        if not match:
            return ""
        return (match.group(1) or "").strip()

    def _fetch_full_text(self, source_url: str) -> str:
        allow_insecure_ssl = bool(getattr(settings, "ALLOW_INSECURE_SSL_FETCH", True))
        try:
            request = Request(
                source_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Connection": "close",
                },
            )
            timeout = max(3, int(getattr(settings, "FULL_ARTICLE_FETCH_TIMEOUT_SECONDS", 8)))
            with urlopen(request, timeout=timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/html" not in content_type:
                    return ""
                html_doc = response.read(700000).decode("utf-8", errors="ignore")
        except ssl.SSLError:
            if not allow_insecure_ssl:
                return ""
            try:
                insecure_context = ssl._create_unverified_context()  # type: ignore[attr-defined]
                with urlopen(request, timeout=timeout, context=insecure_context) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if "text/html" not in content_type:
                        return ""
                    html_doc = response.read(700000).decode("utf-8", errors="ignore")
            except (HTTPError, URLError, TimeoutError, ValueError, ssl.SSLError):
                return ""
        except (HTTPError, URLError, TimeoutError, ValueError):
            return ""

        without_noise = re.sub(r"<script[\s\S]*?</script>", " ", html_doc, flags=re.IGNORECASE)
        without_noise = re.sub(r"<style[\s\S]*?</style>", " ", without_noise, flags=re.IGNORECASE)
        article_match = re.search(r"<article[\s\S]*?</article>", without_noise, flags=re.IGNORECASE)
        scope = article_match.group(0) if article_match else without_noise
        paragraphs = re.findall(r"<p[^>]*>([\s\S]*?)</p>", scope, flags=re.IGNORECASE)
        chunks = []
        for paragraph in paragraphs:
            text = re.sub(r"<[^>]+>", " ", paragraph)
            text = html.unescape(text)
            text = re.sub(r"\s*\[\+\d+\s+chars\]\s*$", "", text).strip()
            text = " ".join(text.split())
            if len(text.split()) >= 8:
                chunks.append(text)
        return "\n\n".join(chunks).strip()
