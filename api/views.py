from django.utils import timezone
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.mail import send_mass_mail
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.response import Response
from rest_framework.generics import ListAPIView, RetrieveAPIView, ListCreateAPIView, RetrieveUpdateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from taggit.models import Tag, TaggedItem
import csv

from api.serializers import (
    ArticleDetailSerializer,
    ArticleListSerializer,
    AnalyticsTopPostSerializer,
    AnalyticsTopSourceSerializer,
    CategoryListSerializer,
    CommentSerializer,
    CurrentUserSerializer,
    LoginSerializer,
    NewsSourceSerializer,
    PipelineFetchSerializer,
    PipelineLimitSerializer,
    PipelineRunSerializer,
    MonitoringTaskSnapshotSerializer,
    NewsletterDigestTriggerSerializer,
    NewsletterEmailSerializer,
    NewsletterSubscriberSerializer,
    PostDetailSerializer,
    PostListSerializer,
    SportsFixtureSerializer,
    SportsTableRowSerializer,
)
from blog.models import Article, Bookmark, Category, Comment, Like, NewsSource, NewsletterSubscriber, Post
from blog.services.launch_readiness import compute_launch_readiness_checks
from blog.views import (
    OPENLIGA_MAIN_LEAGUES,
    _cached_click_count,
    _clear_all_analytics_metrics,
    _fetch_openligadb_endpoint,
    _home_feed_score,
    _monitoring_overview,
    _rank_homepage_posts,
    _retention_summary,
    digest_posts_queryset,
)
from blog.tasks import (
    auto_publish_trusted_articles,
    fetch_all_active_sources,
    fetch_source_articles,
    rollback_auto_published_posts,
    summarize_pending_articles,
    _build_unique_slug,
    _resolve_article_category,
)


class ApiHealthView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response(
            {
                "status": "ok",
                "service": "sudo-blog-api",
                "version": "v1",
                "timestamp": timezone.now().isoformat(),
            }
        )


class PublishedPostListAPIView(ListAPIView):
    serializer_class = PostListSerializer

    def get_queryset(self):
        queryset = (
            Post.published.select_related("category", "author", "source_article__source")
            .prefetch_related("tags")
            .order_by("-publish")
        )
        query = (self.request.query_params.get("q") or "").strip()
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query)
                | Q(body__icontains=query)
                | Q(summary__icontains=query)
                | Q(category__name__icontains=query)
                | Q(tags__name__icontains=query)
            ).distinct()
        return queryset


class PublishedPostDetailAPIView(RetrieveAPIView):
    serializer_class = PostDetailSerializer
    queryset = Post.published.select_related("category", "author", "source_article__source").prefetch_related("tags")


class CategoryListAPIView(ListAPIView):
    serializer_class = CategoryListSerializer

    def get_queryset(self):
        return Category.objects.annotate(
            posts_count=Count("posts", filter=Q(posts__status=Post.Status.PUBLISHED), distinct=True)
        ).order_by("name")


class TagListAPIView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        published_ids = list(Post.published.values_list("id", flat=True))
        if not published_ids:
            return Response([])

        content_type = ContentType.objects.get_for_model(Post)
        tagged_rows = TaggedItem.objects.filter(
            content_type=content_type,
            object_id__in=published_ids,
        ).values_list("tag_id", flat=True)

        tag_counts = {}
        for tag_id in tagged_rows:
            tag_counts[tag_id] = tag_counts.get(tag_id, 0) + 1

        tags = Tag.objects.filter(id__in=tag_counts.keys()).order_by("name")
        payload = [
            {
                "id": tag.id,
                "name": tag.name,
                "slug": tag.slug,
                "posts_count": tag_counts.get(tag.id, 0),
            }
            for tag in tags
        ]
        return Response(payload)


class PostCommentListCreateAPIView(APIView):
    def get(self, request, pk):
        post = get_object_or_404(Post.published, pk=pk)
        comments = post.comments.filter(approved=True).select_related("user")
        serializer = CommentSerializer(comments, many=True)
        return Response(serializer.data)

    def post(self, request, pk):
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication credentials were not provided."}, status=status.HTTP_401_UNAUTHORIZED)

        post = get_object_or_404(Post.published, pk=pk)
        serializer = CommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = Comment.objects.create(
            post=post,
            user=request.user,
            body=serializer.validated_data["body"],
        )
        output = CommentSerializer(comment)
        return Response(output.data, status=status.HTTP_201_CREATED)


class PostLikeAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        post = get_object_or_404(Post.published, pk=pk)
        liked = Like.objects.filter(post=post, user=request.user).exists()
        return Response({"liked": liked, "count": post.likes.count()})

    def post(self, request, pk):
        post = get_object_or_404(Post.published, pk=pk)
        like_qs = Like.objects.filter(post=post, user=request.user)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            Like.objects.create(post=post, user=request.user)
            liked = True
        return Response({"liked": liked, "count": post.likes.count()})


class PostBookmarkAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        post = get_object_or_404(Post.published, pk=pk)
        bookmarked = Bookmark.objects.filter(post=post, user=request.user).exists()
        return Response({"bookmarked": bookmarked, "count": post.bookmarks.count()})

    def post(self, request, pk):
        post = get_object_or_404(Post.published, pk=pk)
        bookmark_qs = Bookmark.objects.filter(post=post, user=request.user)
        if bookmark_qs.exists():
            bookmark_qs.delete()
            bookmarked = False
        else:
            Bookmark.objects.create(post=post, user=request.user)
            bookmarked = True
        return Response({"bookmarked": bookmarked, "count": post.bookmarks.count()})


class BookmarkListAPIView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PostListSerializer

    def get_queryset(self):
        return (
            Post.published.filter(bookmarks__user=self.request.user)
            .select_related("category", "author", "source_article__source")
            .prefetch_related("tags")
            .order_by("-bookmarks__created")
            .distinct()
        )


@method_decorator(ensure_csrf_cookie, name="dispatch")
class AuthCsrfAPIView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response(
            {
                "status": "ok",
                "message": "CSRF cookie set. Send X-CSRFToken on authenticated write requests.",
            }
        )


class AuthLoginAPIView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_login"

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if not user:
            return Response({"detail": "Invalid credentials."}, status=status.HTTP_400_BAD_REQUEST)

        login(request, user)
        payload = CurrentUserSerializer(
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "is_staff": user.is_staff,
            }
        ).data
        return Response({"authenticated": True, "user": payload})


class AuthLogoutAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"authenticated": False})


class AuthSessionAPIView(APIView):
    permission_classes = []

    def get(self, request):
        if not request.user.is_authenticated:
            return Response({"authenticated": False, "user": None})

        payload = CurrentUserSerializer(
            {
                "id": request.user.id,
                "username": request.user.username,
                "email": request.user.email,
                "is_staff": request.user.is_staff,
            }
        ).data
        return Response({"authenticated": True, "user": payload})


class CurrentUserAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        payload = CurrentUserSerializer(
            {
                "id": request.user.id,
                "username": request.user.username,
                "email": request.user.email,
                "is_staff": request.user.is_staff,
            }
        ).data
        return Response(payload)


class StaffOnlyAPIView(APIView):
    permission_classes = [IsAdminUser]


class NewsletterSubscribeAPIView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_public"

    def post(self, request):
        serializer = NewsletterEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()

        subscriber, _ = NewsletterSubscriber.objects.get_or_create(email=email)
        if not subscriber.is_active:
            subscriber.is_active = True
            subscriber.save(update_fields=["is_active", "updated"])

        return Response({"subscribed": True, "email": email})


class NewsletterUnsubscribeAPIView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "newsletter_public"

    def post(self, request):
        serializer = NewsletterEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()

        subscriber = NewsletterSubscriber.objects.filter(email=email).first()
        if subscriber and subscriber.is_active:
            subscriber.is_active = False
            subscriber.save(update_fields=["is_active", "updated"])

        return Response({"unsubscribed": True, "email": email})


class NewsletterDigestTriggerAPIView(StaffOnlyAPIView):
    def post(self, request):
        serializer = NewsletterDigestTriggerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        hours = serializer.validated_data.get("hours", 48)
        limit = serializer.validated_data.get("limit", 8)

        if not getattr(settings, "FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED", True):
            return Response(
                {"triggered": False, "reason": "feature_disabled"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        subscribers = list(
            NewsletterSubscriber.objects.filter(is_active=True)
            .values_list("email", flat=True)
            .order_by("email")
        )
        posts = digest_posts_queryset(hours=hours, limit=limit)

        if not subscribers:
            return Response(
                {
                    "triggered": False,
                    "reason": "no_active_subscribers",
                    "hours": hours,
                    "limit": limit,
                    "subscriber_count": 0,
                    "post_count": len(posts),
                    "sent_count": 0,
                }
            )

        if not posts:
            return Response(
                {
                    "triggered": False,
                    "reason": "no_posts",
                    "hours": hours,
                    "limit": limit,
                    "subscriber_count": len(subscribers),
                    "post_count": 0,
                    "sent_count": 0,
                }
            )

        lines = ["Top stories from Stunning Blog:", ""]
        for idx, post in enumerate(posts, start=1):
            lines.append(f"{idx}. {post.title}")
            lines.append(post.get_absolute_url())
            if post.summary:
                lines.append(post.summary[:180])
            lines.append("")

        body = "\n".join(lines)
        subject = f"Stunning Blog Digest ({timezone.now().date().isoformat()})"
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com")
        messages = [(subject, body, from_email, [email]) for email in subscribers]
        sent_count = send_mass_mail(messages, fail_silently=False)

        timestamp = timezone.now()
        NewsletterSubscriber.objects.filter(email__in=subscribers).update(last_sent_at=timestamp)

        return Response(
            {
                "triggered": True,
                "hours": hours,
                "limit": limit,
                "subscriber_count": len(subscribers),
                "post_count": len(posts),
                "sent_count": sent_count,
            }
        )


class NewsletterSubscriberListAPIView(StaffOnlyAPIView, ListAPIView):
    serializer_class = NewsletterSubscriberSerializer

    def get_queryset(self):
        return NewsletterSubscriber.objects.all().order_by("email")


class NewsletterSubscriberDetailAPIView(StaffOnlyAPIView, RetrieveUpdateAPIView):
    serializer_class = NewsletterSubscriberSerializer
    queryset = NewsletterSubscriber.objects.all()


class NewsSourceListAPIView(StaffOnlyAPIView, ListCreateAPIView):
    serializer_class = NewsSourceSerializer

    def get_queryset(self):
        return NewsSource.objects.all().order_by("name")


class NewsSourceDetailAPIView(StaffOnlyAPIView, RetrieveUpdateDestroyAPIView):
    serializer_class = NewsSourceSerializer
    queryset = NewsSource.objects.all()


class ArticleListAPIView(StaffOnlyAPIView, ListAPIView):
    serializer_class = ArticleListSerializer

    def get_queryset(self):
        queryset = Article.objects.select_related("source").order_by("-fetched_at")
        status_filter = (self.request.query_params.get("status") or "").strip()
        source_id = (self.request.query_params.get("source_id") or "").strip()
        query = (self.request.query_params.get("q") or "").strip()

        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if source_id.isdigit():
            queryset = queryset.filter(source_id=int(source_id))
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query)
                | Q(summary__icontains=query)
                | Q(body__icontains=query)
                | Q(source__name__icontains=query)
                | Q(source_url__icontains=query)
            )
        return queryset


class ArticleQueueAPIView(ArticleListAPIView):
    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(status__in=[Article.Status.INGESTED, Article.Status.SUMMARIZED, Article.Status.PENDING_REVIEW])


class ArticleDetailAPIView(StaffOnlyAPIView, RetrieveAPIView):
    serializer_class = ArticleDetailSerializer
    queryset = Article.objects.select_related("source")


class ArticleModerationAPIView(StaffOnlyAPIView):
    def _serialize(self, article):
        return ArticleDetailSerializer(article).data

    def _publish_article(self, article, author):
        category_cache: dict[str, object] = {}
        publish_dt = article.published_at or timezone.now()
        category = _resolve_article_category(article, category_cache)

        post, created = Post.objects.get_or_create(
            source_article=article,
            defaults={
                "title": article.title,
                "slug": _build_unique_slug(article.title, publish_dt),
                "author": author,
                "body": article.summary or article.body,
                "summary": article.summary,
                "cover_image_url": article.image_url,
                "publish": publish_dt,
                "status": Post.Status.PUBLISHED,
                "auto_generated": True,
                "category": category,
            },
        )

        if not created:
            post.title = article.title
            post.body = article.summary or article.body
            post.summary = article.summary
            post.cover_image_url = article.image_url
            post.publish = publish_dt
            post.status = Post.Status.PUBLISHED
            post.auto_generated = True
            post.author = author if post.author_id != author.id else post.author
            post.category = category
            if not post.slug:
                post.slug = _build_unique_slug(article.title, publish_dt)
            post.save(
                update_fields=[
                    "title",
                    "body",
                    "summary",
                    "cover_image_url",
                    "publish",
                    "status",
                    "auto_generated",
                    "author",
                    "category",
                    "slug",
                ]
            )

        article.status = Article.Status.PUBLISHED
        article.save(update_fields=["status", "updated"])
        return post, created

    def post(self, request, pk, action):
        article = get_object_or_404(Article.objects.select_related("source"), pk=pk)

        if action == "queue":
            if article.status != Article.Status.PUBLISHED:
                article.status = Article.Status.PENDING_REVIEW
                article.save(update_fields=["status", "updated"])
            return Response({"status": "ok", "action": action, "article": self._serialize(article)})

        if action == "reject":
            if article.status != Article.Status.PUBLISHED:
                article.status = Article.Status.REJECTED
                article.save(update_fields=["status", "updated"])
            return Response({"status": "ok", "action": action, "article": self._serialize(article)})

        if action == "review":
            if article.status == Article.Status.PUBLISHED:
                article.status = Article.Status.PENDING_REVIEW
                article.save(update_fields=["status", "updated"])
            return Response({"status": "ok", "action": action, "article": self._serialize(article)})

        if action == "publish":
            post, created = self._publish_article(article, request.user)
            return Response(
                {
                    "status": "ok",
                    "action": action,
                    "created": created,
                    "post": {
                        "id": post.id,
                        "slug": post.slug,
                        "status": post.status,
                    },
                    "article": self._serialize(article),
                }
            )

        return Response({"detail": "Unsupported action."}, status=status.HTTP_400_BAD_REQUEST)


class AnalyticsDashboardAPIView(StaffOnlyAPIView, APIView):
    def get(self, request):
        posts = list(Post.published.select_related("source_article__source").order_by("-publish"))
        sources = list(NewsSource.objects.filter(is_active=True).order_by("name"))
        retention_days = max(1, int(getattr(settings, "ANALYTICS_RETENTION_DAYS", 30)))
        retention_summary = _retention_summary(posts, sources)
        monitoring_overview = _monitoring_overview()

        top_posts = sorted(
            (
                {
                    "post_id": post.id,
                    "title": post.title,
                    "clicks": _cached_click_count("analytics:clicks:post", post.id),
                    "source_clicks": _cached_click_count(
                        "analytics:clicks:source",
                        post.source_article.source_id if post.source_article_id and post.source_article and post.source_article.source_id else 0,
                    ),
                    "publish": post.publish,
                }
                for post in posts
            ),
            key=lambda item: (item["clicks"], item["source_clicks"], item["publish"]),
            reverse=True,
        )[:10]

        top_sources = sorted(
            (
                {
                    "source_id": source.id,
                    "name": source.name,
                    "provider": source.provider,
                    "clicks": _cached_click_count("analytics:clicks:source", source.id),
                    "trust_score": source.trust_score,
                    "updated": source.updated,
                }
                for source in sources
            ),
            key=lambda item: (item["clicks"], item["trust_score"], item["updated"]),
            reverse=True,
        )[:10]

        return Response(
            {
                "canonical_url": request.build_absolute_uri(),
                "top_posts": top_posts,
                "top_sources": top_sources,
                "tracked_posts_count": len(posts),
                "tracked_sources_count": len(sources),
                "total_clicks": sum(item["clicks"] for item in top_posts),
                "analytics_retention_days": retention_days,
                "retention_summary": retention_summary,
                "monitoring_overview": monitoring_overview,
            }
        )


class MonitoringHealthAPIView(StaffOnlyAPIView):
    def get(self, request):
        overview = _monitoring_overview()
        return Response(
            {
                "generated_at": timezone.now().isoformat(),
                "status": overview["status"],
                "alert_count": overview["alert_count"],
                "never_run_count": overview["never_run_count"],
                "tasks": overview["tasks"],
            }
        )


class LaunchReadinessAPIView(StaffOnlyAPIView):
    def get(self, request):
        report = compute_launch_readiness_checks()
        status_label = "ready" if report["fail_count"] == 0 else "needs_attention"
        return Response(
            {
                "generated_at": timezone.now().isoformat(),
                "status": status_label,
                "pass_count": report["pass_count"],
                "fail_count": report["fail_count"],
                "total_count": report["total_count"],
                "checks": report["checks"],
            }
        )


class AnalyticsExportCsvAPIView(StaffOnlyAPIView):
    def get(self, request):
        posts = list(Post.published.select_related("source_article__source").order_by("-publish"))
        sources = list(NewsSource.objects.filter(is_active=True).order_by("name"))

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="analytics-dashboard.csv"'

        writer = csv.writer(response)
        writer.writerow([
            "type",
            "id",
            "title_or_name",
            "clicks",
            "source_clicks",
            "publish_or_updated",
            "meta",
        ])

        for post in posts:
            source_id = post.source_article.source_id if post.source_article_id and post.source_article and post.source_article.source_id else None
            writer.writerow([
                "post",
                post.id,
                post.title,
                _cached_click_count("analytics:clicks:post", post.id),
                _cached_click_count("analytics:clicks:source", source_id) if source_id else 0,
                post.publish.isoformat(),
                post.source_article.source.name if post.source_article_id and post.source_article and post.source_article.source else "",
            ])

        for source in sources:
            writer.writerow([
                "source",
                source.id,
                source.name,
                _cached_click_count("analytics:clicks:source", source.id),
                "",
                source.updated.isoformat(),
                source.get_provider_display(),
            ])

        return response


class AnalyticsTrendingSnapshotAPIView(StaffOnlyAPIView):
    def get(self, request):
        posts = list(
            Post.published.select_related("source_article__source")
            .prefetch_related("tags")
            .annotate(
                like_count=Count("likes", distinct=True),
                comment_count=Count("comments", filter=Q(comments__approved=True), distinct=True),
            )
            .order_by("-publish")[:20]
        )

        ranked = _rank_homepage_posts(posts)[:10]
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="trending-snapshot.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "rank",
            "post_id",
            "title",
            "score",
            "likes",
            "comments",
            "post_clicks",
            "source_clicks",
            "freshness_bonus",
            "source_name",
            "publish_date",
        ])

        for index, post in enumerate(ranked, start=1):
            metrics = _home_feed_score(post)
            writer.writerow([
                index,
                post.id,
                post.title,
                round(metrics["total"], 2),
                metrics["like_count"],
                metrics["comment_count"],
                round(metrics["post_clicks"], 2),
                round(metrics["source_clicks"], 2),
                metrics["freshness"],
                post.source_article.source.name if post.source_article_id and post.source_article and post.source_article.source else "",
                post.publish.date().isoformat(),
            ])

        return response


class AnalyticsResetAPIView(StaffOnlyAPIView):
    def post(self, request):
        confirm = (request.data.get("confirm") or "").strip().lower()
        if confirm != "yes":
            return Response({"reset": False, "reason": "confirmation_required"}, status=status.HTTP_400_BAD_REQUEST)

        _clear_all_analytics_metrics()
        return Response({"reset": True, "message": "All analytics cache entries were cleared."})


class SportsFeedAPIView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        limit = request.query_params.get("limit", "12")
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 12

        queryset = (
            Post.published.select_related("author", "category", "source_article__source")
            .prefetch_related("tags")
            .annotate(
                like_count=Count("likes", distinct=True),
                comment_count=Count("comments", filter=Q(comments__approved=True), distinct=True),
            )
            .filter(Q(source_article__source__provider=NewsSource.Provider.OPENLIGADB))
            .order_by("-publish")[:40]
        )
        ranked = _rank_homepage_posts(list(queryset))[:limit]
        payload = PostListSerializer(ranked, many=True).data
        return Response({"count": len(payload), "results": payload})


class SportsFixturesAPIView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        league = (request.query_params.get("league") or "").strip().lower()
        limit = request.query_params.get("limit", "10")
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 10

        league_map = {item["code"]: item["name"] for item in OPENLIGA_MAIN_LEAGUES}
        if league and league not in league_map:
            return Response({"detail": "Invalid league code."}, status=status.HTTP_400_BAD_REQUEST)

        selected_codes = [league] if league else list(league_map.keys())
        leagues = []
        for code in selected_codes:
            fixtures_raw = _fetch_openligadb_endpoint(f"getmatchdata/{code}")
            fixtures = []
            for item in (fixtures_raw or [])[:limit]:
                team1 = ((item.get("Team1") or {}).get("TeamName") or "Home").strip()
                team2 = ((item.get("Team2") or {}).get("TeamName") or "Away").strip()
                kickoff = (item.get("MatchDateTime") or item.get("MatchDateTimeUTC") or "").strip()
                result_block = item.get("MatchResults") or []
                final_score = result_block[-1] if result_block else {}
                score = ""
                if final_score.get("PointsTeam1") is not None and final_score.get("PointsTeam2") is not None:
                    score = f"{final_score.get('PointsTeam1')} - {final_score.get('PointsTeam2')}"
                fixtures.append({"home": team1, "away": team2, "kickoff": kickoff, "score": score})

            leagues.append(
                {
                    "league": league_map[code],
                    "code": code,
                    "fixtures": SportsFixtureSerializer(fixtures, many=True).data,
                }
            )

        return Response({"leagues": leagues})


class SportsTablesAPIView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        league = (request.query_params.get("league") or "").strip().lower()
        limit = request.query_params.get("limit", "12")
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 12

        league_map = {item["code"]: item["name"] for item in OPENLIGA_MAIN_LEAGUES}
        if league and league not in league_map:
            return Response({"detail": "Invalid league code."}, status=status.HTTP_400_BAD_REQUEST)

        selected_codes = [league] if league else list(league_map.keys())
        leagues = []
        for code in selected_codes:
            tables_raw = _fetch_openligadb_endpoint(f"getbltable/{code}")
            rows = []
            for row in (tables_raw or [])[:limit]:
                rows.append(
                    {
                        "rank": row.get("platz") or row.get("rank") or row.get("position") or "",
                        "team": row.get("teamName") or row.get("team") or "",
                        "points": row.get("points") or row.get("punkte") or 0,
                        "goals": row.get("goals") or row.get("tore") or 0,
                        "conceded": row.get("opponentGoals") or row.get("gegentore") or 0,
                        "matches": row.get("matches") or row.get("spiele") or 0,
                    }
                )

            leagues.append(
                {
                    "league": league_map[code],
                    "code": code,
                    "rows": SportsTableRowSerializer(rows, many=True).data,
                }
            )

        return Response({"leagues": leagues})


class SportsOpenLigaAPIView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        endpoint = (request.query_params.get("endpoint") or "").strip().lower()
        allowed_prefixes = ("getmatchdata/", "getbltable/")
        league_codes = {item["code"] for item in OPENLIGA_MAIN_LEAGUES}

        if not endpoint or not endpoint.startswith(allowed_prefixes):
            return Response({"detail": "Invalid endpoint."}, status=status.HTTP_400_BAD_REQUEST)

        parts = endpoint.split("/", 1)
        if len(parts) != 2 or parts[1] not in league_codes:
            return Response({"detail": "Invalid league code for endpoint."}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"sports:openligadb:{endpoint}"
        cache_hit = cache.get(cache_key) is not None
        data = _fetch_openligadb_endpoint(endpoint)
        return Response({"endpoint": endpoint, "cache_hit": cache_hit, "data": data})


class StaffPipelineAPIView(APIView):
    permission_classes = [IsAdminUser]

    def _run_action(self, action, payload):
        if action == "fetch":
            source_id = payload.get("source_id")
            max_items = payload.get("max_items", 20)
            if source_id is not None:
                return fetch_source_articles(source_id=source_id, max_items=max_items)
            return fetch_all_active_sources(max_items=max_items)
        if action == "summarize":
            return summarize_pending_articles(limit=payload.get("limit", 20))
        if action == "publish":
            return auto_publish_trusted_articles(limit=payload.get("limit", 20))
        if action == "rollback":
            return rollback_auto_published_posts(limit=payload.get("limit", 20))
        raise ValueError(f"Unsupported pipeline action: {action}")


class PipelineFetchAPIView(StaffPipelineAPIView):
    def post(self, request):
        serializer = PipelineFetchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        if payload.get("source_id") is not None:
            result = fetch_source_articles(
                source_id=payload["source_id"],
                max_items=payload.get("max_items", 20),
            )
            action = "fetch-source"
        else:
            result = fetch_all_active_sources(max_items=payload.get("max_items", 20))
            action = "fetch-all"

        return Response({"status": "ok", "action": action, "result": result})


class PipelineSummarizeAPIView(StaffPipelineAPIView):
    def post(self, request):
        serializer = PipelineLimitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = summarize_pending_articles(limit=serializer.validated_data.get("limit", 20))
        return Response({"status": "ok", "action": "summarize", "result": result})


class PipelinePublishAPIView(StaffPipelineAPIView):
    def post(self, request):
        serializer = PipelineLimitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = auto_publish_trusted_articles(limit=serializer.validated_data.get("limit", 20))
        return Response({"status": "ok", "action": "publish", "result": result})


class PipelineRollbackAPIView(StaffPipelineAPIView):
    def post(self, request):
        serializer = PipelineLimitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = rollback_auto_published_posts(limit=serializer.validated_data.get("limit", 20))
        return Response({"status": "ok", "action": "rollback", "result": result})


class PipelineRunAPIView(StaffPipelineAPIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "pipeline_run"

    def post(self, request):
        serializer = PipelineRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        results = []
        for step in serializer.validated_data["steps"]:
            result = self._run_action(step["action"], step)
            results.append({"action": step["action"], "result": result})

        return Response({"status": "ok", "results": results})
