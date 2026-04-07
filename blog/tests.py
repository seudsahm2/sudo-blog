from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from io import StringIO
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.cache import cache

from .models import Article, Bookmark, Comment, Like, NewsSource, NewsletterSubscriber, Post
from .admin import _clear_post_click_metrics, _clear_source_click_metrics
from .services import NewsIngestionService
from .services.summarization import ArticleSummarizationService
from .tasks import (
    auto_publish_trusted_articles,
    fetch_source_articles,
    rollback_auto_published_posts,
    summarize_pending_articles,
)


class NewsSourceModelTests(TestCase):
    def test_source_defaults(self):
        source = NewsSource.objects.create(
            name="Tech Feed",
            provider=NewsSource.Provider.NEWSAPI,
        )

        self.assertTrue(source.is_active)
        self.assertFalse(source.auto_publish)
        self.assertEqual(source.fetch_interval_minutes, 60)
        self.assertEqual(source.trust_score, 50)


class ArticleModelTests(TestCase):
    def test_article_defaults(self):
        source = NewsSource.objects.create(
            name="World Feed",
            provider=NewsSource.Provider.GNEWS,
        )
        article = Article.objects.create(
            source=source,
            title="Sample article",
            body="Body text for summary testing.",
            source_url="https://example.com/sample-article",
        )

        self.assertEqual(article.status, Article.Status.INGESTED)
        self.assertEqual(article.language, "en")
        self.assertTrue(article.is_ad_safe)
        self.assertEqual(article.originality_score, 0)


class NewsIngestionServiceTests(TestCase):
    def setUp(self):
        self.source = NewsSource.objects.create(
            name="API Feed",
            provider=NewsSource.Provider.NEWSAPI,
        )
        self.service = NewsIngestionService()

    def test_fingerprint_is_stable_for_equivalent_whitespace(self):
        hash_a = self.service.fingerprint(
            "Title",
            "Some   body  text",
            "https://example.com/a",
        )
        hash_b = self.service.fingerprint(
            "Different Title",
            "Some body text",
            "https://example.com/a",
        )
        self.assertEqual(hash_a, hash_b)

    def test_ingest_items_creates_and_updates(self):
        items = [
            {
                "title": "First headline",
                "body": "Body one",
                "source_url": "https://example.com/one",
                "external_id": "one",
            }
        ]
        result = self.service.ingest_items(source=self.source, items=items)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(Article.objects.count(), 1)

        items[0]["body"] = "Body one updated"
        result = self.service.ingest_items(source=self.source, items=items)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        self.assertEqual(Article.objects.count(), 1)

    def test_low_quality_article_moves_to_review(self):
        items = [
            {
                "title": "Tiny content",
                "body": "too short",
                "source_url": "https://example.com/low-quality",
                "external_id": "low-quality",
            }
        ]
        self.service.ingest_items(source=self.source, items=items)
        article = Article.objects.get(source_url="https://example.com/low-quality")
        self.assertEqual(article.status, Article.Status.PENDING_REVIEW)

    @override_settings(DISALLOWED_CONTENT_TERMS=["violence"])
    def test_blocked_term_marks_article_rejected(self):
        body = "This article contains violence and must be flagged for ads safety review."
        items = [
            {
                "title": "Policy alert",
                "body": body,
                "source_url": "https://example.com/policy-alert",
                "external_id": "policy-alert",
            }
        ]
        self.service.ingest_items(source=self.source, items=items)
        article = Article.objects.get(source_url="https://example.com/policy-alert")
        self.assertFalse(article.is_ad_safe)
        self.assertEqual(article.status, Article.Status.REJECTED)

    @override_settings(MIN_ARTICLE_WORDS=1, MIN_ORIGINALITY_SCORE=0)
    def test_duplicate_fingerprint_moves_second_article_to_review(self):
        body = " ".join(["word"] * 30)
        first = [
            {
                "title": "First duplicate candidate",
                "body": body,
                "source_url": "https://example.com/dup-a",
                "external_id": "dup-a",
            }
        ]
        second = [
            {
                "title": "Second duplicate candidate",
                "body": body,
                "source_url": "https://example.com/dup-b",
                "external_id": "dup-b",
            }
        ]

        self.service.ingest_items(source=self.source, items=first)
        self.service.ingest_items(source=self.source, items=second)

        article_first = Article.objects.get(source_url="https://example.com/dup-a")
        article_second = Article.objects.get(source_url="https://example.com/dup-b")
        self.assertEqual(article_first.status, Article.Status.INGESTED)
        self.assertEqual(article_second.status, Article.Status.PENDING_REVIEW)

    def test_telegram_ingest_items_force_pending_review(self):
        telegram_source = NewsSource.objects.create(
            name="Telegram Feed",
            provider=NewsSource.Provider.TELEGRAM,
        )
        items = [
            {
                "title": "Telegram update",
                "body": "Useful body content from Telegram source",
                "source_url": "telegram://channel/100",
                "external_id": "100",
            }
        ]
        self.service.ingest_items(source=telegram_source, items=items)
        article = Article.objects.get(source=telegram_source)
        self.assertEqual(article.status, Article.Status.PENDING_REVIEW)

    @override_settings(
        TELEGRAM_BOT_TOKEN="bot-token",
        TELEGRAM_CHAT_ID="-100123",
        TELEGRAM_SOURCE_ITEMS_JSON="[]",
    )
    def test_telegram_adapter_live_api_extracts_matching_chat_messages(self):
        telegram_source = NewsSource.objects.create(
            name="Telegram Live Feed",
            provider=NewsSource.Provider.TELEGRAM,
        )

        payload = {
            "ok": True,
            "result": [
                {
                    "channel_post": {
                        "message_id": 11,
                        "chat": {"id": -100123},
                        "text": "Matching channel update",
                    }
                },
                {
                    "channel_post": {
                        "message_id": 12,
                        "chat": {"id": -100999},
                        "text": "Other channel update",
                    }
                },
            ],
        }

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                import json
                return json.dumps(payload).encode("utf-8")

        adapter = self.service.get_adapter(telegram_source, max_items=5)
        with patch("blog.services.news_ingestion.urlopen", return_value=DummyResponse()):
            parsed = adapter.fetch_payload()

        self.assertIn("items", parsed)
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["items"][0]["external_id"], "11")
        self.assertIn("telegram://-100123/11", parsed["items"][0]["source_url"])


class SummarizationTests(TestCase):
    def setUp(self):
        self.source = NewsSource.objects.create(
            name="Summary Feed",
            provider=NewsSource.Provider.NEWSAPI,
        )

    @override_settings(AI_SUMMARY_PROVIDER="gemini", GEMINI_API_KEY="", GROQ_API_KEY="")
    def test_fallback_summary_is_generated_without_provider_keys(self):
        article = Article.objects.create(
            source=self.source,
            title="Long story",
            body=" ".join(["news"] * 140),
            source_url="https://example.com/summary-story",
        )
        summarizer = ArticleSummarizationService()
        summarizer.summarize_article(article)

        article.refresh_from_db()
        self.assertTrue(article.summary)
        self.assertEqual(article.status, Article.Status.SUMMARIZED)
        self.assertEqual(article.summary_provider, "fallback")
        self.assertEqual(article.summary_model, "extractive")
        self.assertGreater(article.summary_total_tokens, 0)

    @override_settings(AI_SUMMARY_PROVIDER="gemini", GEMINI_API_KEY="", GROQ_API_KEY="")
    def test_summarize_pending_articles_updates_only_ingested(self):
        ingested = Article.objects.create(
            source=self.source,
            title="Ingested story",
            body=" ".join(["content"] * 100),
            source_url="https://example.com/ingested",
            status=Article.Status.INGESTED,
        )
        Article.objects.create(
            source=self.source,
            title="Already summarized",
            body=" ".join(["content"] * 100),
            source_url="https://example.com/summarized",
            status=Article.Status.SUMMARIZED,
        )

        result = summarize_pending_articles(limit=10)

        ingested.refresh_from_db()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summarized"], 1)
        self.assertEqual(ingested.status, Article.Status.SUMMARIZED)

    @override_settings(
        AI_SUMMARY_PROVIDER="groq",
        GROQ_API_KEY="",
        GEMINI_API_KEY="",
        SUMMARIZER_PROMPT_MODE="deep",
    )
    def test_prompt_mode_is_stored_in_summary_metadata(self):
        article = Article.objects.create(
            source=self.source,
            title="Prompt mode story",
            body=" ".join(["content"] * 120),
            source_url="https://example.com/prompt-mode",
            status=Article.Status.INGESTED,
        )

        summarizer = ArticleSummarizationService()
        summarizer.summarize_article(article)

        article.refresh_from_db()
        self.assertEqual(article.summary_prompt_mode, "deep")


class AutoPublishWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="pass1234",
            is_staff=True,
        )
        self.source = NewsSource.objects.create(
            name="Trusted Feed",
            provider=NewsSource.Provider.NEWSAPI,
            auto_publish=True,
            trust_score=90,
        )

    @override_settings(
        AUTO_PUBLISH_MIN_TRUST_SCORE=70,
        AUTO_PUBLISH_MIN_ORIGINALITY=40,
        AUTO_PUBLISH_REQUIRE_AD_SAFE=True,
    )
    def test_auto_publish_trusted_articles_publishes_qualified_content(self):
        article = Article.objects.create(
            source=self.source,
            title="Qualified article",
            body="Full source body text",
            summary="Qualified summary",
            image_url="https://example.com/image.jpg",
            source_url="https://example.com/qualified",
            originality_score=60,
            is_ad_safe=True,
            status=Article.Status.SUMMARIZED,
        )

        result = auto_publish_trusted_articles(limit=10)
        article.refresh_from_db()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["published"], 1)
        self.assertEqual(article.status, Article.Status.PUBLISHED)
        self.assertEqual(Post.objects.filter(source_article=article).count(), 1)
        published_post = Post.objects.get(source_article=article)
        self.assertEqual(published_post.body, "Qualified summary")
        self.assertEqual(published_post.summary, "Qualified summary")
        self.assertEqual(published_post.cover_image_url, "https://example.com/image.jpg")

    @override_settings(
        AUTO_PUBLISH_MIN_TRUST_SCORE=70,
        AUTO_PUBLISH_MIN_ORIGINALITY=40,
        AUTO_PUBLISH_REQUIRE_AD_SAFE=True,
    )
    def test_auto_publish_moves_unqualified_content_to_review(self):
        low_trust_source = NewsSource.objects.create(
            name="Low Trust Feed",
            provider=NewsSource.Provider.GNEWS,
            auto_publish=True,
            trust_score=20,
        )
        article = Article.objects.create(
            source=low_trust_source,
            title="Unqualified article",
            body="body",
            summary="summary",
            source_url="https://example.com/unqualified",
            originality_score=80,
            is_ad_safe=True,
            status=Article.Status.SUMMARIZED,
        )

        result = auto_publish_trusted_articles(limit=10)
        article.refresh_from_db()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["published"], 0)
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(article.status, Article.Status.PENDING_REVIEW)

    def test_rollback_auto_published_posts_reverts_statuses(self):
        article = Article.objects.create(
            source=self.source,
            title="Published article",
            body="body",
            summary="summary",
            source_url="https://example.com/published",
            originality_score=80,
            is_ad_safe=True,
            status=Article.Status.PUBLISHED,
        )
        post = Post.objects.create(
            title="Published post",
            slug="published-post",
            author=self.user,
            body="summary",
            publish=article.fetched_at,
            status=Post.Status.PUBLISHED,
            auto_generated=True,
            source_article=article,
        )

        result = rollback_auto_published_posts(limit=10)
        post.refresh_from_db()
        article.refresh_from_db()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rolled_back"], 1)
        self.assertEqual(post.status, Post.Status.DRAFT)
        self.assertEqual(article.status, Article.Status.PENDING_REVIEW)

    @override_settings(FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED=False)
    def test_fetch_source_articles_skips_when_telegram_flag_disabled(self):
        telegram_source = NewsSource.objects.create(
            name="Telegram Source Disabled",
            provider=NewsSource.Provider.TELEGRAM,
            is_active=True,
        )
        result = fetch_source_articles(source_id=telegram_source.id, max_items=5)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "telegram_feature_disabled")

    @override_settings(
        FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED=True,
        TELEGRAM_SOURCE_ITEMS_JSON='[{"title":"t1","body":"body one","source_url":"telegram://news/1"}]',
        TELEGRAM_FETCH_MAX_ITEMS=1,
        TELEGRAM_FETCH_INTERVAL_MINUTES=60,
    )
    def test_fetch_source_articles_respects_telegram_schedule_window(self):
        telegram_source = NewsSource.objects.create(
            name="Telegram Source Enabled",
            provider=NewsSource.Provider.TELEGRAM,
            is_active=True,
        )
        first = fetch_source_articles(source_id=telegram_source.id, max_items=5)
        second = fetch_source_articles(source_id=telegram_source.id, max_items=5)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["fetched"], 1)
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "telegram_schedule_window")

    @override_settings(
        FEATURE_FLAG_AUTOPUBLISH_ENABLED=True,
        FEATURE_FLAG_TELEGRAM_AUTOPUBLISH_ENABLED=False,
        TELEGRAM_REQUIRE_MANUAL_REVIEW=True,
    )
    def test_auto_publish_blocks_telegram_articles_by_policy(self):
        telegram_source = NewsSource.objects.create(
            name="Trusted Telegram",
            provider=NewsSource.Provider.TELEGRAM,
            auto_publish=True,
            trust_score=95,
        )
        article = Article.objects.create(
            source=telegram_source,
            title="Telegram policy check",
            body="body",
            summary="summary",
            source_url="telegram://news/policy-check",
            originality_score=95,
            is_ad_safe=True,
            status=Article.Status.SUMMARIZED,
        )

        result = auto_publish_trusted_articles(limit=10)
        article.refresh_from_db()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["published"], 0)
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(article.status, Article.Status.PENDING_REVIEW)

    def test_task_monitoring_records_success_metrics(self):
        cache.clear()
        summarize_pending_articles(limit=5)

        self.assertEqual(cache.get("monitoring:task:summarize_pending_articles:last_status"), "ok")
        self.assertEqual(cache.get("monitoring:task:summarize_pending_articles:consecutive_failures"), 0)
        self.assertGreaterEqual(int(cache.get("monitoring:task:summarize_pending_articles:total_runs", 0)), 1)

    def test_task_monitoring_records_failure_metrics(self):
        cache.clear()

        from django.core.exceptions import ObjectDoesNotExist

        with self.assertRaises(ObjectDoesNotExist):
            fetch_source_articles(source_id=999999, max_items=1)

        self.assertEqual(cache.get("monitoring:task:fetch_source_articles:last_status"), "error")
        self.assertGreaterEqual(int(cache.get("monitoring:task:fetch_source_articles:total_failures", 0)), 1)
        self.assertGreaterEqual(int(cache.get("monitoring:task:fetch_source_articles:consecutive_failures", 0)), 1)

    @override_settings(TASK_RETRY_MAX_ATTEMPTS=3, TASK_RETRY_BACKOFF_BASE_SECONDS=1, TASK_RETRY_BACKOFF_MAX_SECONDS=2)
    def test_fetch_source_articles_retries_and_records_retry_metrics(self):
        cache.clear()

        class DummyResult:
            source_name = "Retry Source"
            fetched = 1
            created = 1
            updated = 0

        with patch(
            "blog.tasks.NewsIngestionService.fetch_and_store",
            side_effect=[RuntimeError("temporary failure"), DummyResult()],
        ):
            result = fetch_source_articles(source_id=self.source.id, max_items=1)

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(int(cache.get("monitoring:task:fetch_source_articles:total_retries", 0)), 1)
        self.assertEqual(cache.get("monitoring:task:fetch_source_articles:last_status"), "ok")

    @override_settings(FEATURE_FLAG_INGESTION_ENABLED=False)
    def test_fetch_source_articles_respects_ingestion_feature_flag(self):
        result = fetch_source_articles(source_id=self.source.id, max_items=1)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "feature_disabled")

    @override_settings(FEATURE_FLAG_SUMMARIZATION_ENABLED=False)
    def test_summarize_pending_articles_respects_feature_flag(self):
        result = summarize_pending_articles(limit=1)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "feature_disabled")

    @override_settings(FEATURE_FLAG_AUTOPUBLISH_ENABLED=False)
    def test_auto_publish_respects_feature_flag(self):
        result = auto_publish_trusted_articles(limit=1)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "feature_disabled")

    @override_settings(FEATURE_FLAG_ROLLBACK_ENABLED=False)
    def test_rollback_respects_feature_flag(self):
        result = rollback_auto_published_posts(limit=1)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "feature_disabled")


class SeoAndHomepageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reader",
            email="reader@example.com",
            password="pass1234",
        )
        self.source = NewsSource.objects.create(
            name="SEO Source",
            provider=NewsSource.Provider.NEWSAPI,
        )

        for idx in range(1, 7):
            article = Article.objects.create(
                source=self.source,
                title=f"Article {idx}",
                body="body text",
                summary="summary text",
                source_url=f"https://example.com/article-{idx}",
                status=Article.Status.PUBLISHED,
                originality_score=70,
            )
            Post.objects.create(
                title=f"Post {idx}",
                slug=f"post-{idx}",
                author=self.user,
                body="body",
                summary="summary",
                status=Post.Status.PUBLISHED,
                auto_generated=(idx % 2 == 0),
                source_article=article,
            )

        self.staff_user = get_user_model().objects.create_user(
            username="staffer",
            email="staffer@example.com",
            password="pass1234",
            is_staff=True,
        )

    def test_homepage_includes_ranking_sections(self):
        response = self.client.get(reverse("blog:post_list"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("trending_posts", response.context)
        self.assertIn("fresh_auto_posts", response.context)
        self.assertIn("editor_posts", response.context)
        self.assertTrue(response.context["show_in_feed_ad"])

    @override_settings(
        ADSENSE_ENABLED=True,
        ADSENSE_CLIENT_ID="ca-pub-1234567890",
        ADSENSE_SLOT_HEADER="1111111111",
        ADSENSE_SLOT_IN_FEED="2222222222",
        ADSENSE_SLOT_IN_ARTICLE="3333333333",
        ADSENSE_SLOT_FOOTER="4444444444",
        ADSENSE_SLOT_TRENDING="5555555555",
        ADSENSE_SLOT_STICKY_MOBILE="6666666666",
    )
    def test_adsense_markup_renders_when_enabled(self):
        response = self.client.get(reverse("blog:post_list"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("pagead2.googlesyndication.com", content)
        self.assertIn("data-ad-slot=\"1111111111\"", content)
        self.assertIn("data-ad-slot=\"2222222222\"", content)
        self.assertIn("data-ad-slot=\"4444444444\"", content)
        self.assertIn("data-ad-slot=\"5555555555\"", content)
        self.assertIn("data-ad-slot=\"6666666666\"", content)

    @override_settings(
        ADSENSE_ENABLED=True,
        ADSENSE_CLIENT_ID="ca-pub-1234567890",
        ADSENSE_SLOT_IN_ARTICLE="3333333333",
        ADSENSE_SLOT_BELOW_CONTENT="7777777777",
    )
    def test_adsense_markup_renders_on_detail_page(self):
        target = Post.published.get(slug="post-1")
        response = self.client.get(target.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("pagead2.googlesyndication.com", content)
        self.assertIn("data-ad-slot=\"3333333333\"", content)
        self.assertIn("data-ad-slot=\"7777777777\"", content)

    def test_homepage_trending_respects_source_click_totals(self):
        cache.clear()
        source_post = Post.published.get(slug="post-2")
        other_post = Post.published.get(slug="post-1")

        source_post.likes.create(user=self.user)
        source_post.comments.create(user=self.user, body="Nice", approved=True)
        other_post.likes.create(user=self.user)

        source_id = source_post.source_article.source_id
        cache.set(f"analytics:clicks:source:{source_id}:total", 25, timeout=60 * 60)

        response = self.client.get(reverse("blog:post_list"))
        self.assertEqual(response.status_code, 200)
        trending = list(response.context["trending_posts"])
        self.assertGreaterEqual(len(trending), 2)
        self.assertEqual(trending[0].id, source_post.id)

    def test_source_click_decay_lets_fresh_content_win(self):
        cache.clear()
        stale_post = Post.published.get(slug="post-2")
        fresh_post = Post.published.get(slug="post-1")

        stale_post.likes.create(user=self.user)
        fresh_post.likes.create(user=self.user)

        stale_source_id = stale_post.source_article.source_id
        fresh_source_id = fresh_post.source_article.source_id
        cache.set(f"analytics:clicks:source:{stale_source_id}:total", 100, timeout=60 * 60)
        cache.set(
            f"analytics:clicks:source:{stale_source_id}:last_seen",
            timezone.now() - timedelta(days=60),
            timeout=60 * 60,
        )
        cache.set(f"analytics:clicks:source:{fresh_source_id}:total", 10, timeout=60 * 60)
        cache.set(
            f"analytics:clicks:source:{fresh_source_id}:last_seen",
            timezone.now(),
            timeout=60 * 60,
        )

        cache.set(f"analytics:clicks:post:{stale_post.id}:total", 50, timeout=60 * 60)
        cache.set(
            f"analytics:clicks:post:{stale_post.id}:last_seen",
            timezone.now() - timedelta(days=60),
            timeout=60 * 60,
        )
        cache.set(f"analytics:clicks:post:{fresh_post.id}:total", 10, timeout=60 * 60)
        cache.set(
            f"analytics:clicks:post:{fresh_post.id}:last_seen",
            timezone.now(),
            timeout=60 * 60,
        )

        response = self.client.get(reverse("blog:post_list"))
        self.assertEqual(response.status_code, 200)
        trending = list(response.context["trending_posts"])
        self.assertGreaterEqual(len(trending), 2)
        self.assertEqual(trending[0].id, fresh_post.id)

    def test_search_mode_disables_in_feed_ad_guardrail(self):
        response = self.client.get(reverse("blog:post_list"), {"q": "Post"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["show_in_feed_ad"])

    def test_similar_posts_prioritize_engagement(self):
        target = Post.published.get(slug="post-1")
        top_similar = Post.published.get(slug="post-2")
        other_similar = Post.published.get(slug="post-3")

        target.tags.add("ai")
        top_similar.tags.add("ai")
        other_similar.tags.add("ai")

        Like.objects.create(post=top_similar, user=self.user)
        Comment.objects.create(post=top_similar, user=self.user, body="Great")

        response = self.client.get(target.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        similar = list(response.context["similar_posts"])
        self.assertGreaterEqual(len(similar), 2)
        self.assertEqual(similar[0].id, top_similar.id)

    def test_similar_posts_applies_source_diversity_cap(self):
        target = Post.published.get(slug="post-1")
        target.tags.add("market")

        same_source_posts = [
            Post.published.get(slug="post-2"),
            Post.published.get(slug="post-3"),
            Post.published.get(slug="post-4"),
        ]
        for item in same_source_posts:
            item.tags.add("market")

        other_source = NewsSource.objects.create(
            name="Other Source",
            provider=NewsSource.Provider.GNEWS,
        )
        other_article = Article.objects.create(
            source=other_source,
            title="Other source article",
            body="body",
            summary="summary",
            source_url="https://example.com/other-source-article",
            status=Article.Status.PUBLISHED,
            originality_score=80,
        )
        other_post = Post.objects.create(
            title="Other source post",
            slug="other-source-post",
            author=self.user,
            body="body",
            summary="summary",
            status=Post.Status.PUBLISHED,
            source_article=other_article,
        )
        other_post.tags.add("market")

        response = self.client.get(target.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        similar = list(response.context["similar_posts"])
        same_source_count = sum(1 for post in similar if post.source_article.source_id == self.source.id)
        self.assertLessEqual(same_source_count, 2)

    def test_click_tracking_endpoint_records_counts(self):
        cache.clear()
        post = Post.published.get(slug="post-1")
        post.source_article.source.trust_score = 80
        post.source_article.source.save(update_fields=["trust_score", "updated"])
        response = self.client.post(
            reverse("blog:track_post_click"),
            {"post_id": post.id, "placement": "card-title"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["tracked"])
        self.assertEqual(response.json()["total_clicks"], 1)
        self.assertEqual(response.json()["source_clicks"], 1)

        key = f"analytics:clicks:post:{post.id}:placement:card-title"
        self.assertEqual(cache.get(key), 1)
        source_key = f"analytics:clicks:source:{post.source_article.source_id}:total"
        self.assertEqual(cache.get(source_key), 1)

    def test_bookmark_toggle_endpoint_creates_and_removes(self):
        post = Post.published.get(slug="post-1")
        self.client.force_login(self.user)

        create_response = self.client.post(reverse("blog:post_bookmark", args=[post.id]))
        self.assertEqual(create_response.status_code, 200)
        self.assertTrue(create_response.json()["bookmarked"])
        self.assertEqual(create_response.json()["count"], 1)
        self.assertTrue(Bookmark.objects.filter(post=post, user=self.user).exists())

        remove_response = self.client.post(reverse("blog:post_bookmark", args=[post.id]))
        self.assertEqual(remove_response.status_code, 200)
        self.assertFalse(remove_response.json()["bookmarked"])
        self.assertEqual(remove_response.json()["count"], 0)
        self.assertFalse(Bookmark.objects.filter(post=post, user=self.user).exists())

    def test_bookmark_toggle_requires_authentication(self):
        post = Post.published.get(slug="post-1")
        response = self.client.post(reverse("blog:post_bookmark", args=[post.id]))
        self.assertEqual(response.status_code, 302)

    def test_bookmarks_page_lists_saved_posts(self):
        first = Post.published.get(slug="post-1")
        second = Post.published.get(slug="post-2")
        Bookmark.objects.create(post=first, user=self.user)
        Bookmark.objects.create(post=second, user=self.user)

        self.client.force_login(self.user)
        response = self.client.get(reverse("blog:bookmarks_list"))
        self.assertEqual(response.status_code, 200)
        posts = list(response.context["posts"])
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual(posts[0].id, second.id)
        self.assertEqual(posts[1].id, first.id)

    def test_bookmarks_page_requires_authentication(self):
        response = self.client.get(reverse("blog:bookmarks_list"))
        self.assertEqual(response.status_code, 302)

    def test_newsletter_subscribe_creates_subscriber(self):
        response = self.client.post(reverse("blog:newsletter_subscribe"), {"email": "reader@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["subscribed"])
        self.assertTrue(NewsletterSubscriber.objects.filter(email="reader@example.com", is_active=True).exists())

    def test_newsletter_subscribe_reactivates_existing_email(self):
        NewsletterSubscriber.objects.create(email="reader@example.com", is_active=False)
        response = self.client.post(reverse("blog:newsletter_subscribe"), {"email": "reader@example.com"})
        self.assertEqual(response.status_code, 200)
        subscriber = NewsletterSubscriber.objects.get(email="reader@example.com")
        self.assertTrue(subscriber.is_active)

    def test_newsletter_subscribe_rejects_invalid_email(self):
        response = self.client.post(reverse("blog:newsletter_subscribe"), {"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["subscribed"])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', DEFAULT_FROM_EMAIL='digest@example.com')
    def test_send_newsletter_digest_command_sends_email_and_updates_timestamp(self):
        subscriber = NewsletterSubscriber.objects.create(email="reader@example.com", is_active=True)
        post = Post.published.get(slug="post-1")
        post.summary = "Digest summary"
        post.save(update_fields=["summary", "updated"])

        call_command("send_newsletter_digest", hours=720, limit=3)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Stunning Blog Digest", mail.outbox[0].subject)
        self.assertIn("Top stories from Stunning Blog", mail.outbox[0].body)
        self.assertIn("/blog/", mail.outbox[0].body)
        subscriber.refresh_from_db()
        self.assertIsNotNone(subscriber.last_sent_at)

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='digest@example.com',
        FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED=False,
    )
    def test_send_newsletter_digest_respects_feature_flag(self):
        NewsletterSubscriber.objects.create(email="reader@example.com", is_active=True)
        out = StringIO()

        call_command("send_newsletter_digest", hours=24, limit=3, stdout=out)

        self.assertIn("Newsletter digest feature is disabled.", out.getvalue())
        self.assertEqual(len(mail.outbox), 0)

    def test_social_image_endpoint_serves_svg(self):
        post = Post.published.get(slug="post-1")
        response = self.client.get(post.get_absolute_url().replace(post.slug + "/", post.slug + "/social-image.svg"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn(post.title, response.content.decode("utf-8"))

    def test_tag_social_image_endpoint_serves_svg(self):
        target = Post.published.get(slug="post-1")
        target.tags.add("ai")

        response = self.client.get(reverse("blog:tag_social_image", kwargs={"tag_slug": "ai"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        content = response.content.decode("utf-8")
        self.assertIn("#ai", content)

    def test_tag_list_uses_tag_social_image(self):
        target = Post.published.get(slug="post-1")
        target.tags.add("ai")

        response = self.client.get(reverse("blog:post_list_by_tag", kwargs={"tag_slug": "ai"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tag"].slug, "ai")
        self.assertIn("social_image_url", response.context)
        self.assertIn("/blog/tag/ai/social-image.svg", response.context["social_image_url"])

    def test_robots_txt_endpoint(self):
        response = self.client.get(reverse("blog:robots_txt"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sitemap:", response.content.decode("utf-8"))

    def test_ads_txt_endpoint_uses_adsense_client_id(self):
        with override_settings(ADSENSE_CLIENT_ID="ca-pub-5061735490844182"):
            response = self.client.get(reverse("ads_txt_root"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("google.com, pub-5061735490844182, DIRECT, f08c47fec0942fa0", response.content.decode("utf-8"))

    def test_sitemap_endpoint(self):
        post = Post.published.get(slug="post-1")
        newer_ts = timezone.now() + timedelta(days=1)
        publish_now = timezone.now()
        Post.objects.filter(pk=post.pk).update(publish=publish_now)
        Post.objects.filter(pk=post.pk).update(updated=newer_ts)

        response = self.client.get(reverse("blog:sitemap_xml"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("<urlset", content)
        self.assertIn("/blog/", content)
        self.assertIn(newer_ts.strftime("%Y-%m-%d"), content)
        self.assertIn("xmlns:image", content)
        self.assertIn("xmlns:news", content)
        self.assertIn("<image:image>", content)
        self.assertIn("<news:news>", content)
        self.assertIn("social-image.svg", content)

    def test_legal_page_endpoint(self):
        response = self.client.get(reverse("blog:legal_page", kwargs={"page": "privacy"}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Privacy Policy")

    def test_analytics_dashboard_shows_cached_totals_for_staff(self):
        cache.clear()
        post = Post.published.get(slug="post-1")
        source = post.source_article.source
        cache.set(f"analytics:clicks:post:{post.id}:total", 13, timeout=60 * 60)
        cache.set(f"analytics:clicks:source:{source.id}:total", 42, timeout=60 * 60)

        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("blog:analytics_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, post.title)
        self.assertContains(response, "13")
        self.assertContains(response, source.name)
        self.assertContains(response, "42")

    def test_analytics_dashboard_requires_staff(self):
        response = self.client.get(reverse("blog:analytics_dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_manual_pipeline_endpoint_requires_staff(self):
        response = self.client.post(reverse("blog:run_manual_pipeline"), {"action": "full"})
        self.assertEqual(response.status_code, 302)

    def test_manual_pipeline_endpoint_runs_full_action_for_staff(self):
        self.client.force_login(self.staff_user)

        with patch("blog.views.fetch_all_active_sources", return_value={"status": "ok", "sources": 1}) as fetch_mock, patch(
            "blog.views.summarize_pending_articles", return_value={"status": "ok", "summarized": 3}
        ) as summarize_mock, patch(
            "blog.views.auto_publish_trusted_articles", return_value={"status": "ok", "published": 2, "reviewed": 1}
        ) as publish_mock:
            response = self.client.post(
                reverse("blog:run_manual_pipeline"),
                {
                    "action": "full",
                    "fetch_limit": "11",
                    "summarize_limit": "22",
                    "publish_limit": "33",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("blog:analytics_dashboard"))
        fetch_mock.assert_called_once_with(max_items=11)
        summarize_mock.assert_called_once_with(limit=22)
        publish_mock.assert_called_once_with(limit=33)

        dashboard = self.client.get(reverse("blog:analytics_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Last Manual Run")

    def test_manual_pipeline_endpoint_runs_single_action_for_staff(self):
        self.client.force_login(self.staff_user)

        with patch("blog.views.fetch_all_active_sources") as fetch_mock, patch(
            "blog.views.summarize_pending_articles", return_value={"status": "ok", "summarized": 5}
        ) as summarize_mock, patch("blog.views.auto_publish_trusted_articles") as publish_mock:
            response = self.client.post(
                reverse("blog:run_manual_pipeline"),
                {
                    "action": "summarize",
                    "summarize_limit": "17",
                },
            )

        self.assertEqual(response.status_code, 302)
        summarize_mock.assert_called_once_with(limit=17)
        fetch_mock.assert_not_called()
        publish_mock.assert_not_called()

    @override_settings(ANALYTICS_RETENTION_DAYS=14)
    def test_analytics_dashboard_shows_retention_policy(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("blog:analytics_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TTL window: 14 days")

    def test_analytics_dashboard_shows_monitoring_block(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("blog:analytics_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Monitoring")
        self.assertContains(response, "Open health JSON")

    def test_monitoring_health_requires_staff(self):
        response = self.client.get(reverse("blog:monitoring_health"))
        self.assertEqual(response.status_code, 302)

    def test_monitoring_health_returns_json_for_staff(self):
        cache.set("monitoring:task:summarize_pending_articles:last_status", "ok", timeout=60)
        cache.set("monitoring:task:summarize_pending_articles:total_runs", 3, timeout=60)

        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("blog:monitoring_health"))
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertIn("status", payload)
        self.assertIn("tasks", payload)
        self.assertTrue(any(item["task"] == "summarize_pending_articles" for item in payload["tasks"]))

    def test_analytics_export_csv_is_staff_only(self):
        response = self.client.get(reverse("blog:analytics_export_csv"))
        self.assertEqual(response.status_code, 302)

    def test_analytics_export_csv_contains_post_and_source_rows(self):
        cache.clear()
        post = Post.published.get(slug="post-1")
        source = post.source_article.source
        cache.set(f"analytics:clicks:post:{post.id}:total", 7, timeout=60 * 60)
        cache.set(f"analytics:clicks:source:{source.id}:total", 19, timeout=60 * 60)

        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("blog:analytics_export_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("type,id,title_or_name,clicks,source_clicks,publish_or_updated,meta", content)
        self.assertIn(post.title, content)
        self.assertIn(source.name, content)
        self.assertIn("7", content)
        self.assertIn("19", content)

    def test_analytics_export_trending_snapshot_is_staff_only(self):
        response = self.client.get(reverse("blog:analytics_export_trending_snapshot"))
        self.assertEqual(response.status_code, 302)

    def test_analytics_export_trending_snapshot_contains_ranked_rows(self):
        cache.clear()
        post = Post.published.get(slug="post-1")
        post.likes.create(user=self.user)
        post.comments.create(user=self.user, body="Good", approved=True)
        cache.set(f"analytics:clicks:post:{post.id}:total", 14, timeout=60 * 60)
        cache.set(f"analytics:clicks:post:{post.id}:last_seen", timezone.now(), timeout=60 * 60)

        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("blog:analytics_export_trending_snapshot"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("rank,post_id,title,score,likes,comments,post_clicks,source_clicks,freshness_bonus,source_name,publish_date", content)
        self.assertIn(post.title, content)

    def test_analytics_reset_all_requires_confirmation(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(reverse("blog:analytics_reset_all"), {})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["reset"])
        self.assertEqual(response.json()["reason"], "confirmation_required")

    def test_analytics_cache_reset_helpers_clear_metrics(self):
        cache.clear()
        post = Post.published.get(slug="post-1")
        source = post.source_article.source
        cache.set(f"analytics:clicks:post:{post.id}:total", 8, timeout=60 * 60)
        cache.set(f"analytics:clicks:post:{post.id}:last_seen", timezone.now(), timeout=60 * 60)
        cache.set(f"analytics:clicks:source:{source.id}:total", 21, timeout=60 * 60)
        cache.set(f"analytics:clicks:source:{source.id}:last_seen", timezone.now(), timeout=60 * 60)

        _clear_post_click_metrics(post.id)
        _clear_source_click_metrics(source.id)

        self.assertIsNone(cache.get(f"analytics:clicks:post:{post.id}:total"))
        self.assertIsNone(cache.get(f"analytics:clicks:post:{post.id}:last_seen"))
        self.assertIsNone(cache.get(f"analytics:clicks:source:{source.id}:total"))
        self.assertIsNone(cache.get(f"analytics:clicks:source:{source.id}:last_seen"))

    def test_analytics_reset_all_clears_all_metrics(self):
        cache.clear()
        post = Post.published.get(slug="post-1")
        source = post.source_article.source
        cache.set(f"analytics:clicks:post:{post.id}:total", 8, timeout=60 * 60)
        cache.set(f"analytics:clicks:post:{post.id}:last_seen", timezone.now(), timeout=60 * 60)
        cache.set(f"analytics:clicks:source:{source.id}:total", 21, timeout=60 * 60)
        cache.set(f"analytics:clicks:source:{source.id}:last_seen", timezone.now(), timeout=60 * 60)

        self.client.force_login(self.staff_user)
        response = self.client.post(reverse("blog:analytics_reset_all"), {"confirm": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reset"])
        self.assertIsNone(cache.get(f"analytics:clicks:post:{post.id}:total"))
        self.assertIsNone(cache.get(f"analytics:clicks:post:{post.id}:last_seen"))
        self.assertIsNone(cache.get(f"analytics:clicks:source:{source.id}:total"))
        self.assertIsNone(cache.get(f"analytics:clicks:source:{source.id}:last_seen"))

    @override_settings(ANALYTICS_RETENTION_DAYS=12)
    def test_report_analytics_retention_command_outputs_policy(self):
        out = StringIO()
        call_command("report_analytics_retention", stdout=out)
        output = out.getvalue()
        self.assertIn("ANALYTICS_RETENTION_DAYS=12", output)
        self.assertIn("Published posts tracked:", output)
        self.assertIn("Active sources tracked:", output)

    @override_settings(
        DEBUG=False,
        ALLOWED_HOSTS=["example.com"],
        USE_POSTGRES=True,
        FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED=True,
    )
    def test_launch_readiness_report_outputs_summary(self):
        out = StringIO()
        call_command("launch_readiness_report", stdout=out)
        output = out.getvalue()
        self.assertIn("Launch Readiness Report", output)
        self.assertIn("Summary:", output)
        self.assertIn("[PASS] debug_disabled", output)
        self.assertIn("feature_flag_telegram_ingestion_enabled", output)

    @override_settings(DEBUG=True, ALLOWED_HOSTS=[])
    def test_launch_readiness_report_flags_failures(self):
        out = StringIO()
        call_command("launch_readiness_report", stdout=out)
        output = out.getvalue()
        self.assertIn("[FAIL] debug_disabled", output)
        self.assertIn("[FAIL] allowed_hosts_configured", output)
