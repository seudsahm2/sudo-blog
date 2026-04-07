import hashlib
import html
import json
import re
import ssl
from dataclasses import dataclass
from typing import Dict, List
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from blog.models import Article, NewsSource


@dataclass
class FetchResult:
    source_name: str
    fetched: int
    created: int
    updated: int


class BaseProviderAdapter:
    items_key = ""

    def __init__(self, source: NewsSource, max_items: int = 20):
        self.source = source
        self.max_items = max_items

    def get_api_key(self) -> str:
        provider_to_setting = {
            NewsSource.Provider.NEWSAPI.value: "NEWSAPI_KEY",
            NewsSource.Provider.GNEWS.value: "GNEWS_KEY",
            NewsSource.Provider.MEDIASTACK.value: "MEDIASTACK_KEY",
            NewsSource.Provider.NEWSDATA.value: "NEWSDATA_KEY",
            NewsSource.Provider.GUARDIAN.value: "GUARDIAN_KEY",
        }
        key_name = provider_to_setting.get(self.source.provider)
        return getattr(settings, key_name, "") if key_name else ""

    def build_url(self) -> str:
        raise NotImplementedError

    def parse_items(self, payload: Dict) -> List[Dict]:
        raise NotImplementedError

    def fetch_payload(self) -> Dict:
        url = self.build_url()
        request = Request(url, headers={"User-Agent": "sudo-blog-ingestor/1.0"})
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def clean_text(self, text: str) -> str:
        cleaned = (text or "").strip()
        # Remove provider truncation markers like "... [+4505 chars]".
        cleaned = re.sub(r"\s*\[\+\d+\s+chars\]\s*$", "", cleaned)
        cleaned = cleaned.replace("\x00", " ")
        return " ".join(cleaned.split())

    def _fetch_html(self, source_url: str) -> str:
        timeout = max(3, int(getattr(settings, "FULL_ARTICLE_FETCH_TIMEOUT_SECONDS", 8)))
        request = Request(
            source_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "close",
            },
        )
        allow_insecure_ssl = bool(getattr(settings, "ALLOW_INSECURE_SSL_FETCH", True))
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "text/html" not in content_type:
                    return ""
                raw = response.read(600000)
        except ssl.SSLError:
            if not allow_insecure_ssl:
                return ""
            try:
                insecure_context = ssl._create_unverified_context()
                with urlopen(request, timeout=timeout, context=insecure_context) as response:
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if "text/html" not in content_type:
                        return ""
                    raw = response.read(600000)
            except (HTTPError, URLError, TimeoutError, ValueError, ssl.SSLError):
                return ""
        except (HTTPError, URLError, TimeoutError, ValueError):
            return ""
        return raw.decode("utf-8", errors="ignore")

    def _extract_og_image(self, html_content: str) -> str:
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html_content, re.IGNORECASE)
            if match:
                return (match.group(1) or "").strip()
        return ""

    def _extract_readable_text(self, html_content: str) -> str:
        if not html_content:
            return ""
        without_noise = re.sub(r"<script[\s\S]*?</script>", " ", html_content, flags=re.IGNORECASE)
        without_noise = re.sub(r"<style[\s\S]*?</style>", " ", without_noise, flags=re.IGNORECASE)
        article_match = re.search(r"<article[\s\S]*?</article>", without_noise, flags=re.IGNORECASE)
        scope = article_match.group(0) if article_match else without_noise
        paragraphs = re.findall(r"<p[^>]*>([\s\S]*?)</p>", scope, flags=re.IGNORECASE)
        if not paragraphs:
            return ""
        text_chunks = []
        for paragraph in paragraphs:
            plain = re.sub(r"<[^>]+>", " ", paragraph)
            plain = html.unescape(plain)
            plain = self.clean_text(plain)
            if len(plain.split()) >= 8:
                text_chunks.append(plain)
        full_text = "\n\n".join(text_chunks)
        return full_text.strip()

    def enrich_content(self, source_url: str, body: str, image_url: str = "") -> tuple[str, str]:
        clean_body = self.clean_text(body)
        should_fetch_full = bool(getattr(settings, "FETCH_FULL_ARTICLE_CONTENT", True))
        min_words_for_full = max(1, int(getattr(settings, "FULL_ARTICLE_MIN_WORDS", 140)))
        needs_fulltext = (
            "[+" in (body or "")
            or len(clean_body.split()) < min_words_for_full
        )

        if not should_fetch_full or not needs_fulltext:
            return clean_body, image_url

        html_content = self._fetch_html(source_url)
        if not html_content:
            return clean_body, image_url

        full_text = self._extract_readable_text(html_content)
        final_body = full_text if len(full_text.split()) >= len(clean_body.split()) else clean_body
        final_image = image_url or self._extract_og_image(html_content)
        return final_body, final_image


class NewsApiAdapter(BaseProviderAdapter):
    items_key = "articles"

    def build_url(self) -> str:
        country = (getattr(settings, "NEWSAPI_COUNTRY", "us") or "us").strip()
        query = (getattr(settings, "NEWSAPI_QUERY", "") or "").strip()
        params = {
            "apiKey": self.get_api_key(),
            "pageSize": self.max_items,
        }
        if country:
            params["country"] = country
        if query:
            params["q"] = query
        base = self.source.base_url or "https://newsapi.org/v2/top-headlines"
        return f"{base}?{urlencode(params)}"

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for item in items:
            title = (item.get("title") or "").strip()
            body = (item.get("content") or item.get("description") or "").strip()
            source_url = (item.get("url") or "").strip()
            image_url = (item.get("urlToImage") or "").strip()
            if not (title and body and source_url):
                continue
            body, image_url = self.enrich_content(source_url=source_url, body=body, image_url=image_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                    "source_url": source_url,
                    "external_id": source_url,
                }
            )
        return results


class GNewsAdapter(BaseProviderAdapter):
    items_key = "articles"

    def build_url(self) -> str:
        country = (getattr(settings, "GNEWS_COUNTRY", "us") or "us").strip()
        topic = (getattr(settings, "GNEWS_TOPIC", "") or "").strip()
        query = (getattr(settings, "GNEWS_QUERY", "") or "").strip()
        params = {
            "apikey": self.get_api_key(),
            "lang": "en",
            "max": self.max_items,
        }
        if country:
            params["country"] = country
        if topic:
            params["topic"] = topic
        if query:
            params["q"] = query
        base = self.source.base_url or "https://gnews.io/api/v4/top-headlines"
        return f"{base}?{urlencode(params)}"

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for item in items:
            title = (item.get("title") or "").strip()
            body = (item.get("content") or item.get("description") or "").strip()
            source_url = (item.get("url") or "").strip()
            image_url = (item.get("image") or "").strip()
            if not (title and body and source_url):
                continue
            body, image_url = self.enrich_content(source_url=source_url, body=body, image_url=image_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                    "source_url": source_url,
                    "external_id": source_url,
                }
            )
        return results


class MediaStackAdapter(BaseProviderAdapter):
    items_key = "data"

    def build_url(self) -> str:
        params = {
            "access_key": self.get_api_key(),
            "languages": "en",
            "limit": self.max_items,
        }
        base = self.source.base_url or "http://api.mediastack.com/v1/news"
        return f"{base}?{urlencode(params)}"

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for item in items:
            title = (item.get("title") or "").strip()
            body = (item.get("description") or item.get("title") or "").strip()
            source_url = (item.get("url") or "").strip()
            image_url = (item.get("image") or "").strip()
            if not (title and body and source_url):
                continue
            body, image_url = self.enrich_content(source_url=source_url, body=body, image_url=image_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                    "source_url": source_url,
                    "external_id": source_url,
                }
            )
        return results


class NewsDataAdapter(BaseProviderAdapter):
    items_key = "results"

    def build_url(self) -> str:
        country = (getattr(settings, "NEWSDATA_COUNTRY", "us") or "us").strip()
        query = (getattr(settings, "NEWSDATA_QUERY", "") or "").strip()
        params = {
            "apikey": self.get_api_key(),
            "language": "en",
            "size": self.max_items,
        }
        if country:
            params["country"] = country
        if query:
            params["q"] = query
        base = self.source.base_url or "https://newsdata.io/api/1/news"
        return f"{base}?{urlencode(params)}"

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for item in items:
            title = (item.get("title") or "").strip()
            body = (item.get("content") or item.get("description") or "").strip()
            source_url = (item.get("link") or "").strip()
            image_url = (item.get("image_url") or "").strip()
            if not (title and body and source_url):
                continue
            body, image_url = self.enrich_content(source_url=source_url, body=body, image_url=image_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                    "source_url": source_url,
                    "external_id": item.get("article_id") or source_url,
                }
            )
        return results


class GuardianAdapter(BaseProviderAdapter):
    items_key = "results"

    def build_url(self) -> str:
        params = {
            "api-key": self.get_api_key(),
            "show-fields": "trailText,bodyText,thumbnail,headline",
            "page-size": self.max_items,
            "order-by": "newest",
        }
        base = self.source.base_url or "https://content.guardianapis.com/search"
        return f"{base}?{urlencode(params)}"

    def parse_items(self, payload: Dict) -> List[Dict]:
        response = payload.get("response", {}) if isinstance(payload, dict) else {}
        items = response.get(self.items_key, [])
        results = []
        for item in items:
            fields = item.get("fields", {}) or {}
            title = (fields.get("headline") or item.get("webTitle") or "").strip()
            body = (fields.get("bodyText") or fields.get("trailText") or "").strip()
            source_url = (item.get("webUrl") or "").strip()
            image_url = (fields.get("thumbnail") or "").strip()
            if not (title and body and source_url):
                continue
            body, image_url = self.enrich_content(source_url=source_url, body=body, image_url=image_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                    "source_url": source_url,
                    "external_id": item.get("id") or source_url,
                }
            )
        return results


class SpaceflightNewsAdapter(BaseProviderAdapter):
    items_key = "results"

    def build_url(self) -> str:
        params = {
            "limit": self.max_items,
            "ordering": "-published_at",
        }
        base = self.source.base_url or "https://api.spaceflightnewsapi.net/v4/articles/"
        return f"{base}?{urlencode(params)}"

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for item in items:
            title = (item.get("title") or "").strip()
            body = (item.get("summary") or "").strip()
            source_url = (item.get("url") or "").strip()
            image_url = (item.get("image_url") or "").strip()
            if not (title and body and source_url):
                continue
            body, image_url = self.enrich_content(source_url=source_url, body=body, image_url=image_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": image_url,
                    "source_url": source_url,
                    "external_id": str(item.get("id") or source_url),
                }
            )
        return results


class OpenLigaDbAdapter(BaseProviderAdapter):
    items_key = "matches"

    def build_url(self) -> str:
        return self.source.base_url or "https://api.openligadb.de/getmatchdata/bl1"

    def fetch_payload(self) -> Dict:
        url = self.build_url()
        request = Request(url, headers={"User-Agent": "sudo-blog-ingestor/1.0"})
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        if isinstance(data, list):
            return {self.items_key: data[: self.max_items]}
        return {self.items_key: []}

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for item in items:
            team1 = ((item.get("Team1") or {}).get("TeamName") or "Home team").strip()
            team2 = ((item.get("Team2") or {}).get("TeamName") or "Away team").strip()
            result_block = None
            match_results = item.get("MatchResults") or []
            if match_results:
                result_block = match_results[-1]
            score1 = result_block.get("PointsTeam1") if result_block else None
            score2 = result_block.get("PointsTeam2") if result_block else None
            if score1 is not None and score2 is not None:
                title = f"{team1} vs {team2}: {score1}-{score2}"
            else:
                title = f"{team1} vs {team2}"

            kickoff = (item.get("MatchDateTimeUTC") or item.get("MatchDateTime") or "").strip()
            group = ((item.get("Group") or {}).get("GroupName") or "League").strip()
            body = (
                f"Sports update from {group}. Fixture: {team1} against {team2}. "
                f"Kickoff schedule: {kickoff or 'TBD'}. "
                "Track match momentum, standings impact, and recent team form in one place."
            )
            external_id = str(item.get("MatchID") or "")
            if not external_id:
                continue
            source_url = f"https://www.openligadb.de/match/{external_id}"
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": "",
                    "source_url": source_url,
                    "external_id": external_id,
                }
            )
        return results


class TelegramAdapter(BaseProviderAdapter):
    items_key = "items"

    def build_url(self) -> str:
        return self.source.base_url or ""

    def fetch_payload(self) -> Dict:
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = str(getattr(settings, "TELEGRAM_CHAT_ID", "")).strip()
        source_items = getattr(settings, "TELEGRAM_SOURCE_ITEMS_JSON", "[]")

        if self.source.base_url:
            return super().fetch_payload()

        if bot_token and chat_id:
            base_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            url = f"{base_url}?{urlencode({'limit': self.max_items})}"
            request = Request(url, headers={"User-Agent": "sudo-blog-telegram-ingestor/1.0"})
            timeout = max(5, int(getattr(settings, "TELEGRAM_API_TIMEOUT_SECONDS", 20)))
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))

            results = payload.get("result", []) if isinstance(payload, dict) else []
            extracted = []
            for event in results:
                message = event.get("channel_post") or event.get("message") or {}
                message_chat_id = str((message.get("chat") or {}).get("id") or "")
                if message_chat_id != chat_id:
                    continue

                text = (message.get("text") or message.get("caption") or "").strip()
                if not text:
                    continue
                message_id = str(message.get("message_id") or "")
                extracted.append(
                    {
                        "title": text[:80] or "Telegram update",
                        "body": text,
                        "source_url": f"telegram://{chat_id}/{message_id or '0'}",
                        "external_id": message_id or f"tg-{chat_id}-{hash(text)}",
                    }
                )
            return {self.items_key: extracted}

        try:
            parsed = json.loads(source_items)
        except (TypeError, ValueError):
            parsed = []
        return {self.items_key: parsed if isinstance(parsed, list) else []}

    def parse_items(self, payload: Dict) -> List[Dict]:
        items = payload.get(self.items_key, [])
        results = []
        for idx, item in enumerate(items[: self.max_items], start=1):
            title = (item.get("title") or "").strip()
            body = (item.get("body") or item.get("text") or "").strip()
            source_url = (item.get("source_url") or item.get("url") or "").strip()

            if not source_url:
                source_url = f"telegram://{self.source.pk}/{idx}"
            if not title:
                title = (body[:80] or f"Telegram item {idx}").strip()
            if not (title and body and source_url):
                continue

            external_id = str(item.get("external_id") or item.get("message_id") or source_url)
            results.append(
                {
                    "title": title,
                    "body": body,
                    "image_url": "",
                    "source_url": source_url,
                    "external_id": external_id,
                }
            )
        return results


class NewsIngestionService:
    ADAPTERS = {
        NewsSource.Provider.NEWSAPI.value: NewsApiAdapter,
        NewsSource.Provider.GNEWS.value: GNewsAdapter,
        NewsSource.Provider.MEDIASTACK.value: MediaStackAdapter,
        NewsSource.Provider.NEWSDATA.value: NewsDataAdapter,
        NewsSource.Provider.GUARDIAN.value: GuardianAdapter,
        NewsSource.Provider.SPACEFLIGHT.value: SpaceflightNewsAdapter,
        NewsSource.Provider.OPENLIGADB.value: OpenLigaDbAdapter,
        NewsSource.Provider.TELEGRAM.value: TelegramAdapter,
    }

    def get_adapter(self, source: NewsSource, max_items: int = 20) -> BaseProviderAdapter:
        adapter_cls = self.ADAPTERS.get(source.provider)
        if not adapter_cls:
            raise ValueError(f"Unsupported provider for ingestion: {source.provider}")
        return adapter_cls(source=source, max_items=max_items)

    def fingerprint(self, _title: str, body: str, _source_url: str) -> str:
        normalized = " ".join(body.lower().split())
        raw = normalized
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def calculate_originality_score(self, body: str) -> int:
        words = [word.strip(".,!?:;()[]{}\"'").lower() for word in body.split()]
        words = [word for word in words if word]
        if not words:
            return 0
        unique_ratio = len(set(words)) / len(words)
        return min(100, int(unique_ratio * 100))

    def evaluate_quality(self, source: NewsSource, body: str, originality_score: int) -> tuple[bool, bool]:
        min_words = getattr(settings, 'MIN_ARTICLE_WORDS', 120)
        if source.provider in {
            NewsSource.Provider.NEWSAPI,
            NewsSource.Provider.GNEWS,
            NewsSource.Provider.MEDIASTACK,
            NewsSource.Provider.NEWSDATA,
            NewsSource.Provider.GUARDIAN,
            NewsSource.Provider.SPACEFLIGHT,
            NewsSource.Provider.OPENLIGADB,
        }:
            min_words = max(
                1,
                int(getattr(settings, 'EXTERNAL_NEWS_MIN_ARTICLE_WORDS', 20)),
            )
        min_originality = getattr(settings, 'MIN_ORIGINALITY_SCORE', 35)
        blocked_terms = getattr(settings, 'DISALLOWED_CONTENT_TERMS', [])

        word_count = len(body.split())
        lower_body = body.lower()
        has_blocked_term = any(term in lower_body for term in blocked_terms)

        quality_ok = word_count >= min_words and originality_score >= min_originality
        ad_safe = not has_blocked_term
        return quality_ok, ad_safe

    @transaction.atomic
    def ingest_items(self, source: NewsSource, items: List[Dict]) -> FetchResult:
        created = 0
        updated = 0

        for item in items:
            title = item["title"]
            body = item["body"]
            source_url = item["source_url"]
            content_hash = self.fingerprint(title, body, source_url)
            originality_score = self.calculate_originality_score(body)
            quality_ok, ad_safe = self.evaluate_quality(source, body, originality_score)

            has_duplicate_fingerprint = Article.objects.filter(
                content_hash=content_hash,
            ).exclude(source_url=source_url).exists()

            status = Article.Status.INGESTED
            if not quality_ok:
                status = Article.Status.PENDING_REVIEW
            if has_duplicate_fingerprint:
                status = Article.Status.PENDING_REVIEW
            if not ad_safe:
                status = Article.Status.REJECTED
            if source.provider == NewsSource.Provider.TELEGRAM and status != Article.Status.REJECTED:
                status = Article.Status.PENDING_REVIEW

            defaults = {
                "title": title,
                "slug": slugify(title)[:255],
                "body": body,
                "image_url": item.get("image_url", ""),
                "external_id": item.get("external_id", ""),
                "content_hash": content_hash,
                "originality_score": originality_score,
                "is_ad_safe": ad_safe,
                "status": status,
                "fetched_at": timezone.now(),
            }
            article, is_created = Article.objects.update_or_create(
                source_url=source_url,
                defaults={**defaults, "source": source},
            )
            if is_created:
                created += 1
            else:
                updated += 1
                if article.status == Article.Status.PUBLISHED:
                    article.status = Article.Status.INGESTED
                    article.save(update_fields=["status", "updated"])

        return FetchResult(
            source_name=source.name,
            fetched=len(items),
            created=created,
            updated=updated,
        )

    def fetch_and_store(self, source: NewsSource, max_items: int = 20) -> FetchResult:
        adapter = self.get_adapter(source, max_items=max_items)
        payload = adapter.fetch_payload()
        items = adapter.parse_items(payload)
        return self.ingest_items(source, items)
