from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core import mail
from django.conf import settings
from django.test import override_settings
from unittest.mock import patch
from django.utils import timezone

from blog.models import Article, Category, NewsSource, NewsletterSubscriber, Post


class ApiHealthTests(APITestCase):
    def test_health_endpoint_returns_api_status(self):
        response = self.client.get(reverse("api:health"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), "ok")
        self.assertEqual(response.data.get("service"), "sudo-blog-api")
        self.assertEqual(response.data.get("version"), "v1")
        self.assertIn("timestamp", response.data)


class ApiPhaseOneReadTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="api-author",
            email="api-author@example.com",
            password="pass1234",
        )
        self.tech = Category.objects.create(name="Tech", slug="tech")
        self.world = Category.objects.create(name="World", slug="world")

        self.published_tech = Post.objects.create(
            title="AI chip makers rally",
            slug="ai-chip-makers-rally",
            author=self.user,
            body="AI and software markets moved strongly today.",
            summary="Markets rally on AI earnings.",
            status=Post.Status.PUBLISHED,
            category=self.tech,
        )
        self.published_tech.tags.add("ai", "markets")

        self.published_world = Post.objects.create(
            title="Global policy update",
            slug="global-policy-update",
            author=self.user,
            body="Government and diplomacy update from world leaders.",
            summary="Diplomatic summit highlights.",
            status=Post.Status.PUBLISHED,
            category=self.world,
        )
        self.published_world.tags.add("world")

        self.draft_post = Post.objects.create(
            title="Draft should stay hidden",
            slug="draft-should-stay-hidden",
            author=self.user,
            body="This is draft content only.",
            summary="Hidden draft.",
            status=Post.Status.DRAFT,
            category=self.tech,
        )
        self.draft_post.tags.add("draft-only")

    def test_posts_list_returns_published_items_with_pagination_envelope(self):
        response = self.client.get(reverse("api:posts-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("count", response.data)
        self.assertIn("results", response.data)
        self.assertEqual(response.data["count"], 2)
        titles = [item["title"] for item in response.data["results"]]
        self.assertIn("AI chip makers rally", titles)
        self.assertIn("Global policy update", titles)
        self.assertNotIn("Draft should stay hidden", titles)

    def test_posts_list_supports_search_query(self):
        response = self.client.get(reverse("api:posts-list"), {"q": "chip"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["title"], "AI chip makers rally")

    def test_post_detail_returns_published_post(self):
        response = self.client.get(reverse("api:posts-detail", kwargs={"pk": self.published_world.pk}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Global policy update")
        self.assertEqual(response.data["category"]["name"], "World")
        self.assertIn("world", response.data["tags"])

    def test_post_detail_returns_404_for_draft_post(self):
        response = self.client.get(reverse("api:posts-detail", kwargs={"pk": self.draft_post.pk}))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_categories_list_includes_published_posts_count(self):
        response = self.client.get(reverse("api:categories-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {item["slug"]: item for item in response.data["results"]}
        self.assertEqual(rows["tech"]["posts_count"], 1)
        self.assertEqual(rows["world"]["posts_count"], 1)

    def test_tags_list_excludes_draft_only_tags(self):
        response = self.client.get(reverse("api:tags-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slugs = {item["slug"] for item in response.data}
        self.assertIn("ai", slugs)
        self.assertIn("world", slugs)
        self.assertNotIn("draft-only", slugs)


class ApiPhaseTwoInteractionTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="interaction-user",
            email="interaction@example.com",
            password="pass1234",
        )
        self.category = Category.objects.create(name="Tech", slug="tech-phase2")
        self.post = Post.objects.create(
            title="Interaction post",
            slug="interaction-post",
            author=self.user,
            body="Interaction body text",
            summary="Interaction summary",
            status=Post.Status.PUBLISHED,
            category=self.category,
        )

    def test_comments_get_returns_empty_array_initially(self):
        response = self.client.get(reverse("api:posts-comments", kwargs={"pk": self.post.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_comments_post_requires_authentication(self):
        response = self.client.post(
            reverse("api:posts-comments", kwargs={"pk": self.post.pk}),
            {"body": "Hello API"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_comments_post_creates_comment_when_authenticated(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            reverse("api:posts-comments", kwargs={"pk": self.post.pk}),
            {"body": "Great article"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["body"], "Great article")
        self.assertEqual(response.data["user"], "interaction-user")

        list_response = self.client.get(reverse("api:posts-comments", kwargs={"pk": self.post.pk}))
        self.assertEqual(len(list_response.data), 1)

    def test_like_toggle_and_status(self):
        self.client.force_authenticate(user=self.user)

        initial = self.client.get(reverse("api:posts-like", kwargs={"pk": self.post.pk}))
        self.assertEqual(initial.status_code, status.HTTP_200_OK)
        self.assertEqual(initial.data["liked"], False)
        self.assertEqual(initial.data["count"], 0)

        liked = self.client.post(reverse("api:posts-like", kwargs={"pk": self.post.pk}), {}, format="json")
        self.assertEqual(liked.status_code, status.HTTP_200_OK)
        self.assertEqual(liked.data["liked"], True)
        self.assertEqual(liked.data["count"], 1)

        unliked = self.client.post(reverse("api:posts-like", kwargs={"pk": self.post.pk}), {}, format="json")
        self.assertEqual(unliked.status_code, status.HTTP_200_OK)
        self.assertEqual(unliked.data["liked"], False)
        self.assertEqual(unliked.data["count"], 0)

    def test_bookmark_toggle_status_and_list(self):
        self.client.force_authenticate(user=self.user)

        initial = self.client.get(reverse("api:posts-bookmark", kwargs={"pk": self.post.pk}))
        self.assertEqual(initial.status_code, status.HTTP_200_OK)
        self.assertEqual(initial.data["bookmarked"], False)
        self.assertEqual(initial.data["count"], 0)

        bookmarked = self.client.post(reverse("api:posts-bookmark", kwargs={"pk": self.post.pk}), {}, format="json")
        self.assertEqual(bookmarked.status_code, status.HTTP_200_OK)
        self.assertEqual(bookmarked.data["bookmarked"], True)
        self.assertEqual(bookmarked.data["count"], 1)

        list_response = self.client.get(reverse("api:bookmarks-list"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(list_response.data["results"][0]["id"], self.post.id)

        unbookmarked = self.client.post(reverse("api:posts-bookmark", kwargs={"pk": self.post.pk}), {}, format="json")
        self.assertEqual(unbookmarked.status_code, status.HTTP_200_OK)
        self.assertEqual(unbookmarked.data["bookmarked"], False)
        self.assertEqual(unbookmarked.data["count"], 0)

    def test_bookmarks_list_requires_authentication(self):
        response = self.client.get(reverse("api:bookmarks-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ApiPhaseThreeAuthTests(APITestCase):
    def setUp(self):
        self.user_password = "pass1234"
        self.user = get_user_model().objects.create_user(
            username="auth-user",
            email="auth@example.com",
            password=self.user_password,
            is_staff=True,
        )

    def test_csrf_endpoint_sets_cookie(self):
        response = self.client.get(reverse("api:auth-csrf"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("status"), "ok")
        self.assertIn("csrftoken", response.cookies)

    def test_login_success_creates_authenticated_session(self):
        response = self.client.post(
            reverse("api:auth-login"),
            {"username": "auth-user", "password": self.user_password},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["authenticated"])
        self.assertEqual(response.data["user"]["username"], "auth-user")

        session_response = self.client.get(reverse("api:auth-session"))
        self.assertEqual(session_response.status_code, status.HTTP_200_OK)
        self.assertTrue(session_response.data["authenticated"])
        self.assertEqual(session_response.data["user"]["username"], "auth-user")

    def test_login_invalid_credentials_returns_400(self):
        response = self.client.post(
            reverse("api:auth-login"),
            {"username": "auth-user", "password": "wrong-password"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", response.data)

    def test_users_me_requires_auth(self):
        response = self.client.get(reverse("api:users-me"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_users_me_returns_profile_when_authenticated(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("api:users-me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "auth-user")
        self.assertEqual(response.data["email"], "auth@example.com")
        self.assertTrue(response.data["is_staff"])

    def test_logout_clears_session(self):
        self.client.post(
            reverse("api:auth-login"),
            {"username": "auth-user", "password": self.user_password},
            format="json",
        )
        before = self.client.get(reverse("api:auth-session"))
        self.assertTrue(before.data["authenticated"])

        logout_response = self.client.post(reverse("api:auth-logout"), {}, format="json")
        self.assertEqual(logout_response.status_code, status.HTTP_200_OK)
        self.assertFalse(logout_response.data["authenticated"])

        after = self.client.get(reverse("api:auth-session"))
        self.assertEqual(after.status_code, status.HTTP_200_OK)
        self.assertFalse(after.data["authenticated"])


class ApiPhaseFourPipelineTests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.staff_user = user_model.objects.create_user(
            username="pipeline-staff",
            email="pipeline-staff@example.com",
            password="pass1234",
            is_staff=True,
        )
        self.regular_user = user_model.objects.create_user(
            username="pipeline-user",
            email="pipeline-user@example.com",
            password="pass1234",
        )

    def test_pipeline_endpoints_require_staff_access(self):
        self.client.force_authenticate(user=self.regular_user)

        response = self.client.post(
            reverse("api:pipeline-fetch"),
            {"max_items": 5},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("api.views.fetch_all_active_sources")
    @patch("api.views.fetch_source_articles")
    def test_fetch_pipeline_uses_all_sources_when_no_source_is_provided(self, fetch_source_mock, fetch_all_mock):
        fetch_all_mock.return_value = {"status": "ok", "sources": 2}
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:pipeline-fetch"),
            {"max_items": 7},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")
        self.assertEqual(response.data["action"], "fetch-all")
        self.assertEqual(response.data["result"], {"status": "ok", "sources": 2})
        fetch_all_mock.assert_called_once_with(max_items=7)
        fetch_source_mock.assert_not_called()

    @patch("api.views.fetch_all_active_sources")
    @patch("api.views.fetch_source_articles")
    def test_fetch_pipeline_targets_one_source_when_source_is_provided(self, fetch_source_mock, fetch_all_mock):
        fetch_source_mock.return_value = {"status": "ok", "source_id": 9}
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:pipeline-fetch"),
            {"source_id": 9, "max_items": 3},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action"], "fetch-source")
        self.assertEqual(response.data["result"], {"status": "ok", "source_id": 9})
        fetch_source_mock.assert_called_once_with(source_id=9, max_items=3)
        fetch_all_mock.assert_not_called()

    @patch("api.views.summarize_pending_articles")
    def test_summarize_pipeline_endpoint(self, summarize_mock):
        summarize_mock.return_value = {"status": "ok", "summarized": 4}
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:pipeline-summarize"),
            {"limit": 4},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action"], "summarize")
        self.assertEqual(response.data["result"], {"status": "ok", "summarized": 4})
        summarize_mock.assert_called_once_with(limit=4)

    @patch("api.views.auto_publish_trusted_articles")
    def test_publish_pipeline_endpoint(self, publish_mock):
        publish_mock.return_value = {"status": "ok", "published": 2}
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:pipeline-publish"),
            {"limit": 2},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action"], "publish")
        self.assertEqual(response.data["result"], {"status": "ok", "published": 2})
        publish_mock.assert_called_once_with(limit=2)

    @patch("api.views.rollback_auto_published_posts")
    def test_rollback_pipeline_endpoint(self, rollback_mock):
        rollback_mock.return_value = {"status": "ok", "rolled_back": 1}
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:pipeline-rollback"),
            {"limit": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["action"], "rollback")
        self.assertEqual(response.data["result"], {"status": "ok", "rolled_back": 1})
        rollback_mock.assert_called_once_with(limit=1)

    @patch("api.views.rollback_auto_published_posts")
    @patch("api.views.auto_publish_trusted_articles")
    @patch("api.views.summarize_pending_articles")
    @patch("api.views.fetch_all_active_sources")
    def test_pipeline_run_executes_steps_in_order(
        self,
        fetch_all_mock,
        summarize_mock,
        publish_mock,
        rollback_mock,
    ):
        fetch_all_mock.return_value = {"status": "ok", "sources": 3}
        summarize_mock.return_value = {"status": "ok", "summarized": 2}
        publish_mock.return_value = {"status": "ok", "published": 1}
        rollback_mock.return_value = {"status": "ok", "rolled_back": 1}
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:pipeline-run"),
            {
                "steps": [
                    {"action": "fetch", "max_items": 5},
                    {"action": "summarize", "limit": 2},
                    {"action": "publish", "limit": 1},
                    {"action": "rollback", "limit": 1},
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["action"] for row in response.data["results"]], ["fetch", "summarize", "publish", "rollback"])
        self.assertEqual(response.data["results"][0]["result"], {"status": "ok", "sources": 3})
        self.assertEqual(response.data["results"][1]["result"], {"status": "ok", "summarized": 2})
        self.assertEqual(response.data["results"][2]["result"], {"status": "ok", "published": 1})
        self.assertEqual(response.data["results"][3]["result"], {"status": "ok", "rolled_back": 1})
        fetch_all_mock.assert_called_once_with(max_items=5)
        summarize_mock.assert_called_once_with(limit=2)
        publish_mock.assert_called_once_with(limit=1)
        rollback_mock.assert_called_once_with(limit=1)


class ApiPhaseFiveManagementTests(APITestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="phase5-staff",
            email="phase5-staff@example.com",
            password="pass1234",
            is_staff=True,
        )
        self.regular_user = get_user_model().objects.create_user(
            username="phase5-user",
            email="phase5-user@example.com",
            password="pass1234",
        )

    def _create_source(self):
        return NewsSource.objects.create(
            name="Phase 5 Source",
            provider=NewsSource.Provider.CUSTOM,
            is_active=True,
            auto_publish=True,
            trust_score=85,
            fetch_interval_minutes=30,
            base_url="https://example.com/news",
            notes="Test source",
        )

    def _create_article(self, source, *, title, slug_suffix, status=Article.Status.SUMMARIZED, summary_category="Tech"):
        return Article.objects.create(
            source=source,
            title=title,
            slug=f"{slug_suffix}",
            body=f"{title} body text",
            image_url="",
            summary=f"{title} summary",
            summary_provider="gemini",
            summary_model="gemini-2.0-flash",
            summary_category=summary_category,
            summary_prompt_mode="brief",
            summary_prompt_tokens=120,
            summary_completion_tokens=40,
            summary_total_tokens=160,
            summary_estimated_cost_usd="0.000120",
            source_url=f"https://example.com/{slug_suffix}",
            external_id=slug_suffix,
            status=status,
            originality_score=78,
            is_ad_safe=True,
            language="en",
        )

    def test_news_source_crud_requires_staff_access(self):
        self.client.force_authenticate(user=self.regular_user)

        response = self.client.get(reverse("api:news-sources-list"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_news_source_crud_list_create_update_and_delete(self):
        self.client.force_authenticate(user=self.staff_user)

        create_response = self.client.post(
            reverse("api:news-sources-list"),
            {
                "name": "Phase 5 Source",
                "provider": NewsSource.Provider.CUSTOM,
                "is_active": True,
                "auto_publish": True,
                "trust_score": 85,
                "fetch_interval_minutes": 30,
                "base_url": "https://example.com/news",
                "notes": "Test source",
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_response.data["name"], "Phase 5 Source")
        self.assertEqual(create_response.data["provider"], NewsSource.Provider.CUSTOM)

        list_response = self.client.get(reverse("api:news-sources-list"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 1)

        source_id = create_response.data["id"]
        detail_response = self.client.patch(
            reverse("api:news-sources-detail", kwargs={"pk": source_id}),
            {"trust_score": 95, "notes": "Updated notes"},
            format="json",
        )
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data["trust_score"], 95)
        self.assertEqual(detail_response.data["notes"], "Updated notes")

        delete_response = self.client.delete(reverse("api:news-sources-detail", kwargs={"pk": source_id}))
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

        missing_response = self.client.get(reverse("api:news-sources-detail", kwargs={"pk": source_id}))
        self.assertEqual(missing_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_article_list_detail_and_queue_expose_summary_metadata(self):
        self.client.force_authenticate(user=self.staff_user)
        source = self._create_source()
        article = self._create_article(source, title="Phase 5 Article", slug_suffix="phase-5-article")

        list_response = self.client.get(reverse("api:articles-list"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(list_response.data["results"][0]["summary_provider"], "gemini")
        self.assertEqual(list_response.data["results"][0]["summary_category"], "Tech")

        queue_response = self.client.get(reverse("api:articles-queue"))
        self.assertEqual(queue_response.status_code, status.HTTP_200_OK)
        self.assertEqual(queue_response.data["count"], 1)

        detail_response = self.client.get(reverse("api:articles-detail", kwargs={"pk": article.pk}))
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data["summary_model"], "gemini-2.0-flash")
        self.assertEqual(detail_response.data["source"]["name"], "Phase 5 Source")
        self.assertEqual(detail_response.data["body"], "Phase 5 Article body text")

    def test_article_moderation_actions_update_article_and_create_post(self):
        self.client.force_authenticate(user=self.staff_user)
        source = self._create_source()
        queue_article = self._create_article(
            source,
            title="Queue Article",
            slug_suffix="queue-article",
            status=Article.Status.INGESTED,
        )
        publish_article = self._create_article(
            source,
            title="Publish Article",
            slug_suffix="publish-article",
            status=Article.Status.SUMMARIZED,
        )

        queue_response = self.client.post(reverse("api:articles-action", kwargs={"pk": queue_article.pk, "action": "queue"}), {}, format="json")
        self.assertEqual(queue_response.status_code, status.HTTP_200_OK)
        queue_article.refresh_from_db()
        self.assertEqual(queue_article.status, Article.Status.PENDING_REVIEW)

        reject_response = self.client.post(reverse("api:articles-action", kwargs={"pk": queue_article.pk, "action": "reject"}), {}, format="json")
        self.assertEqual(reject_response.status_code, status.HTTP_200_OK)
        queue_article.refresh_from_db()
        self.assertEqual(queue_article.status, Article.Status.REJECTED)

        publish_response = self.client.post(reverse("api:articles-action", kwargs={"pk": publish_article.pk, "action": "publish"}), {}, format="json")
        self.assertEqual(publish_response.status_code, status.HTTP_200_OK)
        self.assertTrue(publish_response.data["created"])
        self.assertEqual(publish_response.data["post"]["status"], Post.Status.PUBLISHED)
        publish_article.refresh_from_db()
        self.assertEqual(publish_article.status, Article.Status.PUBLISHED)
        self.assertTrue(Post.objects.filter(source_article=publish_article).exists())

        review_response = self.client.post(reverse("api:articles-action", kwargs={"pk": publish_article.pk, "action": "review"}), {}, format="json")
        self.assertEqual(review_response.status_code, status.HTTP_200_OK)
        publish_article.refresh_from_db()
        self.assertEqual(publish_article.status, Article.Status.PENDING_REVIEW)


class ApiPhaseSixAnalyticsTests(APITestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="phase6-staff",
            email="phase6-staff@example.com",
            password="pass1234",
            is_staff=True,
        )
        self.category = Category.objects.create(name="Tech", slug="phase6-tech")
        self.source = NewsSource.objects.create(
            name="Phase 6 Source",
            provider=NewsSource.Provider.CUSTOM,
            is_active=True,
            auto_publish=True,
            trust_score=90,
            fetch_interval_minutes=30,
            base_url="https://example.com/feed",
            notes="Analytics source",
        )
        self.author = get_user_model().objects.create_user(
            username="phase6-author",
            email="phase6-author@example.com",
            password="pass1234",
        )
        self.article = Article.objects.create(
            source=self.source,
            title="Analytics article",
            slug="analytics-article",
            body="Analytics article body text",
            image_url="",
            summary="Analytics article summary",
            summary_provider="gemini",
            summary_model="gemini-2.0-flash",
            summary_category="Tech",
            summary_prompt_mode="brief",
            summary_prompt_tokens=100,
            summary_completion_tokens=40,
            summary_total_tokens=140,
            summary_estimated_cost_usd="0.000100",
            source_url="https://example.com/analytics-article",
            external_id="analytics-article",
            status=Article.Status.SUMMARIZED,
            published_at=timezone.now(),
            originality_score=80,
            is_ad_safe=True,
            language="en",
        )
        self.post = Post.objects.create(
            title="Analytics post",
            slug="analytics-post",
            author=self.author,
            body="Analytics post body text",
            summary="Analytics post summary",
            status=Post.Status.PUBLISHED,
            category=self.category,
            publish=timezone.now(),
            auto_generated=True,
            source_article=self.article,
        )

    def test_analytics_dashboard_returns_summary_payload(self):
        cache.set(f"analytics:clicks:post:{self.post.id}:total", 13, timeout=60)
        cache.set(f"analytics:clicks:source:{self.source.id}:total", 42, timeout=60)
        cache.set(f"analytics:clicks:source:{self.source.id}:last_seen", timezone.now(), timeout=60)

        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(reverse("api:analytics-dashboard"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["tracked_posts_count"], 1)
        self.assertEqual(response.data["tracked_sources_count"], 1)
        self.assertEqual(response.data["top_posts"][0]["title"], "Analytics post")
        self.assertEqual(response.data["top_posts"][0]["clicks"], 13)
        self.assertEqual(response.data["top_sources"][0]["name"], "Phase 6 Source")
        self.assertEqual(response.data["top_sources"][0]["clicks"], 42)
        self.assertIn("retention_summary", response.data)
        self.assertIn("monitoring_overview", response.data)

    def test_monitoring_health_returns_task_overview(self):
        cache.set("monitoring:task:summarize_pending_articles:last_status", "ok", timeout=60)
        cache.set("monitoring:task:summarize_pending_articles:total_runs", 3, timeout=60)

        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(reverse("api:analytics-health"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("generated_at", response.data)
        self.assertEqual(response.data["status"], "healthy")
        self.assertTrue(any(item["task"] == "summarize_pending_articles" for item in response.data["tasks"]))

    def test_launch_readiness_returns_report(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(reverse("api:analytics-launch-readiness"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("generated_at", response.data)
        self.assertIn("checks", response.data)
        self.assertIn("pass_count", response.data)
        self.assertIn("fail_count", response.data)

    def test_export_csv_contains_post_and_source_rows(self):
        cache.set(f"analytics:clicks:post:{self.post.id}:total", 7, timeout=60)
        cache.set(f"analytics:clicks:source:{self.source.id}:total", 19, timeout=60)

        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(reverse("api:analytics-export-csv"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("type,id,title_or_name,clicks,source_clicks,publish_or_updated,meta", content)
        self.assertIn("Analytics post", content)
        self.assertIn("Phase 6 Source", content)

    def test_trending_snapshot_export_contains_ranked_rows(self):
        cache.set(f"analytics:clicks:post:{self.post.id}:total", 14, timeout=60)
        cache.set(f"analytics:clicks:post:{self.post.id}:last_seen", timezone.now(), timeout=60)

        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(reverse("api:analytics-trending-snapshot"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("rank,post_id,title,score,likes,comments,post_clicks,source_clicks,freshness_bonus,source_name,publish_date", content)
        self.assertIn("Analytics post", content)

    def test_analytics_reset_requires_confirmation(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(reverse("api:analytics-reset"), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["reset"])
        self.assertEqual(response.data["reason"], "confirmation_required")

    def test_analytics_reset_clears_cache(self):
        cache.set(f"analytics:clicks:post:{self.post.id}:total", 8, timeout=60)
        cache.set(f"analytics:clicks:post:{self.post.id}:last_seen", timezone.now(), timeout=60)
        cache.set(f"analytics:clicks:source:{self.source.id}:total", 21, timeout=60)
        cache.set(f"analytics:clicks:source:{self.source.id}:last_seen", timezone.now(), timeout=60)

        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(reverse("api:analytics-reset"), {"confirm": "yes"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["reset"])
        self.assertIsNone(cache.get(f"analytics:clicks:post:{self.post.id}:total"))
        self.assertIsNone(cache.get(f"analytics:clicks:post:{self.post.id}:last_seen"))
        self.assertIsNone(cache.get(f"analytics:clicks:source:{self.source.id}:total"))
        self.assertIsNone(cache.get(f"analytics:clicks:source:{self.source.id}:last_seen"))


class ApiPhaseSevenSportsTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="phase7-author",
            email="phase7-author@example.com",
            password="pass1234",
        )
        self.category = Category.objects.create(name="Sport", slug="phase7-sport")
        self.sports_source = NewsSource.objects.create(
            name="OpenLiga Feed",
            provider=NewsSource.Provider.OPENLIGADB,
            is_active=True,
            auto_publish=True,
            trust_score=80,
            fetch_interval_minutes=30,
            base_url="https://api.openligadb.de",
            notes="Sports source",
        )
        self.article = Article.objects.create(
            source=self.sports_source,
            title="Matchday update",
            slug="matchday-update",
            body="Matchday update body",
            summary="Matchday summary",
            source_url="https://example.com/matchday-update",
            status=Article.Status.PUBLISHED,
            originality_score=75,
            is_ad_safe=True,
            language="en",
        )
        self.post = Post.objects.create(
            title="Bundesliga Matchday",
            slug="bundesliga-matchday",
            author=self.user,
            body="Bundesliga post body",
            summary="Bundesliga post summary",
            status=Post.Status.PUBLISHED,
            category=self.category,
            publish=timezone.now(),
            auto_generated=True,
            source_article=self.article,
        )

    def test_sports_feed_returns_openligadb_posts(self):
        response = self.client.get(reverse("api:sports-feed"), {"limit": 5})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["title"], "Bundesliga Matchday")

    @patch("api.views._fetch_openligadb_endpoint")
    def test_sports_fixtures_returns_structured_payload(self, fetch_mock):
        fetch_mock.return_value = [
            {
                "Team1": {"TeamName": "Team A"},
                "Team2": {"TeamName": "Team B"},
                "MatchDateTime": "2026-04-07T18:00:00",
                "MatchResults": [{"PointsTeam1": 2, "PointsTeam2": 1}],
            }
        ]

        response = self.client.get(reverse("api:sports-fixtures"), {"league": "pl", "limit": 5})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["leagues"]), 1)
        self.assertEqual(response.data["leagues"][0]["code"], "pl")
        fixture = response.data["leagues"][0]["fixtures"][0]
        self.assertEqual(fixture["home"], "Team A")
        self.assertEqual(fixture["away"], "Team B")
        self.assertEqual(fixture["score"], "2 - 1")

    @patch("api.views._fetch_openligadb_endpoint")
    def test_sports_tables_returns_standings_rows(self, fetch_mock):
        fetch_mock.return_value = [
            {
                "platz": 1,
                "teamName": "Team A",
                "points": 55,
                "goals": 45,
                "opponentGoals": 20,
                "matches": 24,
            }
        ]

        response = self.client.get(reverse("api:sports-tables"), {"league": "bl1", "limit": 5})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["leagues"]), 1)
        row = response.data["leagues"][0]["rows"][0]
        self.assertEqual(row["team"], "Team A")
        self.assertEqual(row["points"], 55)

    def test_sports_openliga_rejects_invalid_endpoint(self):
        response = self.client.get(reverse("api:sports-openliga"), {"endpoint": "getfoo/pl"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("api.views._fetch_openligadb_endpoint")
    def test_sports_openliga_reports_cache_hit_when_key_exists(self, fetch_mock):
        endpoint = "getmatchdata/pl"
        cache.set(f"sports:openligadb:{endpoint}", [{"cached": True}], timeout=60)
        fetch_mock.return_value = [{"sample": True}]

        response = self.client.get(reverse("api:sports-openliga"), {"endpoint": endpoint})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["cache_hit"])
        self.assertEqual(response.data["endpoint"], endpoint)
        self.assertEqual(response.data["data"], [{"sample": True}])


class ApiPhaseEightNewsletterTests(APITestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="phase8-staff",
            email="phase8-staff@example.com",
            password="pass1234",
            is_staff=True,
        )
        self.regular_user = get_user_model().objects.create_user(
            username="phase8-user",
            email="phase8-user@example.com",
            password="pass1234",
        )
        self.author = get_user_model().objects.create_user(
            username="phase8-author",
            email="phase8-author@example.com",
            password="pass1234",
        )
        self.category = Category.objects.create(name="Phase8", slug="phase8")
        self.post = Post.objects.create(
            title="Digest candidate",
            slug="digest-candidate",
            author=self.author,
            body="Digest body",
            summary="Digest summary",
            status=Post.Status.PUBLISHED,
            category=self.category,
            publish=timezone.now(),
        )

    def test_subscribe_creates_or_reactivates_subscriber(self):
        response = self.client.post(
            reverse("api:newsletter-subscribe"),
            {"email": "Reader@Example.COM"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["subscribed"])
        self.assertEqual(response.data["email"], "reader@example.com")
        self.assertTrue(NewsletterSubscriber.objects.filter(email="reader@example.com", is_active=True).exists())

        subscriber = NewsletterSubscriber.objects.get(email="reader@example.com")
        subscriber.is_active = False
        subscriber.save(update_fields=["is_active", "updated"])

        second = self.client.post(
            reverse("api:newsletter-subscribe"),
            {"email": "reader@example.com"},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        subscriber.refresh_from_db()
        self.assertTrue(subscriber.is_active)

    def test_unsubscribe_is_idempotent(self):
        NewsletterSubscriber.objects.create(email="reader@example.com", is_active=True)

        response = self.client.post(
            reverse("api:newsletter-unsubscribe"),
            {"email": "reader@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["unsubscribed"])
        self.assertEqual(response.data["email"], "reader@example.com")
        self.assertFalse(NewsletterSubscriber.objects.get(email="reader@example.com").is_active)

        second = self.client.post(
            reverse("api:newsletter-unsubscribe"),
            {"email": "reader@example.com"},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data["unsubscribed"])

    def test_digest_trigger_requires_staff(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            reverse("api:newsletter-digest-trigger"),
            {"hours": 48, "limit": 5},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="digest@example.com",
        FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED=True,
    )
    def test_digest_trigger_sends_email_and_updates_last_sent(self):
        subscriber = NewsletterSubscriber.objects.create(email="reader@example.com", is_active=True)
        self.client.force_authenticate(user=self.staff_user)

        response = self.client.post(
            reverse("api:newsletter-digest-trigger"),
            {"hours": 720, "limit": 5},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["triggered"])
        self.assertEqual(response.data["subscriber_count"], 1)
        self.assertGreaterEqual(response.data["post_count"], 1)
        self.assertEqual(response.data["sent_count"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Stunning Blog Digest", mail.outbox[0].subject)

        subscriber.refresh_from_db()
        self.assertIsNotNone(subscriber.last_sent_at)

    @override_settings(FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED=False)
    def test_digest_trigger_respects_feature_flag(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(
            reverse("api:newsletter-digest-trigger"),
            {"hours": 48, "limit": 5},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["triggered"])
        self.assertEqual(response.data["reason"], "feature_disabled")

    def test_staff_can_list_and_patch_subscribers(self):
        self.client.force_authenticate(user=self.staff_user)
        subscriber = NewsletterSubscriber.objects.create(email="reader@example.com", is_active=True)

        list_response = self.client.get(reverse("api:newsletter-subscribers-list"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(list_response.data["results"][0]["email"], "reader@example.com")

        patch_response = self.client.patch(
            reverse("api:newsletter-subscribers-detail", kwargs={"pk": subscriber.pk}),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        self.assertFalse(patch_response.data["is_active"])

        subscriber.refresh_from_db()
        self.assertFalse(subscriber.is_active)


class ApiPhaseNineSecurityTests(APITestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="phase9-staff",
            email="phase9-staff@example.com",
            password="pass1234",
            is_staff=True,
        )

    def test_pipeline_run_rejects_more_than_ten_steps(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(
            reverse("api:pipeline-run"),
            {"steps": [{"action": "fetch", "max_items": 1} for _ in range(11)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("steps", response.data)

    def test_pipeline_fetch_rejects_excessive_max_items(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(
            reverse("api:pipeline-fetch"),
            {"max_items": 1000},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("max_items", response.data)

    def test_newsletter_digest_trigger_rejects_excessive_window_and_limit(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(
            reverse("api:newsletter-digest-trigger"),
            {"hours": 2000, "limit": 200},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hours", response.data)
        self.assertIn("limit", response.data)

    def test_auth_login_has_scoped_throttle_configuration(self):
        from api.views import AuthLoginAPIView

        self.assertEqual(AuthLoginAPIView.throttle_scope, "auth_login")
        self.assertTrue(AuthLoginAPIView.throttle_classes)
        self.assertIn("auth_login", settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
