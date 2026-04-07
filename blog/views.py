from django.shortcuts import render, get_object_or_404, redirect
from .models import Bookmark, Like, NewsSource, NewsletterSubscriber, Post
from django.db.models import Q
from django.db.models import Case, When, Value, IntegerField, F, ExpressionWrapper
from django.http import HttpResponse, JsonResponse
from django.core.cache import cache
from django.views.generic import ListView
from .forms import CommentForm, EmailPostForm, NewsletterSubscriptionForm
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.views.decorators.http import require_GET
from django.views.decorators.csrf import csrf_exempt
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from xml.sax.saxutils import escape
from datetime import timedelta
from math import exp
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import csv

from taggit.models import Tag
from django.db.models import Count
from blog.services.launch_readiness import compute_launch_readiness_checks
from blog.tasks import fetch_all_active_sources, summarize_pending_articles, auto_publish_trusted_articles


MONITORED_TASKS = [
    'fetch_source_articles',
    'fetch_all_active_sources',
    'summarize_pending_articles',
    'auto_publish_trusted_articles',
    'rollback_auto_published_posts',
]


OPENLIGA_MAIN_LEAGUES = [
    {"code": "bl1", "name": "Bundesliga"},
    {"code": "pl", "name": "Premier League"},
    {"code": "laliga", "name": "La Liga"},
    {"code": "sa", "name": "Serie A"},
    {"code": "cl", "name": "Champions League"},
]


def _fetch_openligadb_endpoint(endpoint):
    cache_key = f"sports:openligadb:{endpoint}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://api.openligadb.de/{endpoint}"
    request = Request(url, headers={"User-Agent": "sudo-blog-sports-hub/1.0"})
    try:
        with urlopen(request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        data = []

    cache.set(cache_key, data, timeout=5 * 60)
    return data


def _enforce_source_diversity(posts, max_per_source=2, limit=4):
    selected = []
    source_counts = {}

    for post in posts:
        source_id = None
        if post.source_article_id and post.source_article:
            source_id = post.source_article.source_id

        if source_id is None:
            selected.append(post)
        else:
            current = source_counts.get(source_id, 0)
            if current < max_per_source:
                selected.append(post)
                source_counts[source_id] = current + 1

        if len(selected) >= limit:
            return selected[:limit]

    return selected[:limit]


def _social_image_url(request, post):
    return request.build_absolute_uri(
        reverse('blog:post_social_image', kwargs={
            'year': post.publish.year,
            'month': post.publish.month,
            'day': post.publish.day,
            'post': post.slug,
        })
    )


def _tag_social_image_url(request, tag):
    return request.build_absolute_uri(
        reverse('blog:tag_social_image', kwargs={'tag_slug': tag.slug})
    )


def _post_freshness_bonus(post):
    age_days = max(0, (timezone.now() - post.publish).days)
    if age_days <= 7:
        return 12
    if age_days <= 30:
        return 6
    return 0


def _source_click_totals(posts):
    source_ids = {
        post.source_article.source_id
        for post in posts
        if post.source_article_id and post.source_article and post.source_article.source_id
    }
    return {
        source_id: int(cache.get(f'analytics:clicks:source:{source_id}:total', 0))
        for source_id in source_ids
    }


def _source_click_signal(source_id):
    total = int(cache.get(f'analytics:clicks:source:{source_id}:total', 0))
    last_seen = cache.get(f'analytics:clicks:source:{source_id}:last_seen')
    if not total or not last_seen:
        return total

    age_days = max(0.0, (timezone.now() - last_seen).total_seconds() / 86400.0)
    half_life_days = 14.0
    decay = exp(-age_days / half_life_days)
    return total * decay


def _post_click_signal(post_id):
    total = int(cache.get(f'analytics:clicks:post:{post_id}:total', 0))
    last_seen = cache.get(f'analytics:clicks:post:{post_id}:last_seen')
    if not total or not last_seen:
        return total

    age_days = max(0.0, (timezone.now() - last_seen).total_seconds() / 86400.0)
    half_life_days = 14.0
    decay = exp(-age_days / half_life_days)
    return total * decay


def _rank_homepage_posts(posts):
    def score(post):
        source_id = None
        if post.source_article_id and post.source_article and post.source_article.source_id:
            source_id = post.source_article.source_id
        source_clicks = _source_click_signal(source_id) if source_id else 0
        post_clicks = _post_click_signal(post.id)
        like_count = getattr(post, 'like_count', 0)
        comment_count = getattr(post, 'comment_count', 0)
        return (
            like_count * 5
            + comment_count * 3
            + post_clicks * 4
            + source_clicks * 2
            + _post_freshness_bonus(post)
        )

    ranked = sorted(posts, key=lambda post: (score(post), post.publish), reverse=True)
    return ranked


def _home_feed_score(post):
    source_id = None
    if post.source_article_id and post.source_article and post.source_article.source_id:
        source_id = post.source_article.source_id
    source_clicks = _source_click_signal(source_id) if source_id else 0
    post_clicks = _post_click_signal(post.id)
    like_count = getattr(post, 'like_count', 0)
    comment_count = getattr(post, 'comment_count', 0)
    freshness = _post_freshness_bonus(post)
    total = like_count * 5 + comment_count * 3 + post_clicks * 4 + source_clicks * 2 + freshness
    return {
        'like_count': like_count,
        'comment_count': comment_count,
        'post_clicks': post_clicks,
        'source_clicks': source_clicks,
        'freshness': freshness,
        'total': total,
    }


def _cached_click_count(key_prefix, obj_id):
    return int(cache.get(f'{key_prefix}:{obj_id}:total', 0))


def _clear_all_analytics_metrics():
    post_ids = Post.published.values_list('id', flat=True)
    source_ids = NewsSource.objects.filter(is_active=True).values_list('id', flat=True)

    for post_id in post_ids:
        cache.delete(f'analytics:clicks:post:{post_id}:total')
        cache.delete(f'analytics:clicks:post:{post_id}:last_seen')

    for source_id in source_ids:
        cache.delete(f'analytics:clicks:source:{source_id}:total')
        cache.delete(f'analytics:clicks:source:{source_id}:last_seen')


def _analytics_retention_seconds():
    retention_days = max(1, int(getattr(settings, 'ANALYTICS_RETENTION_DAYS', 30)))
    return retention_days * 24 * 60 * 60


def _retention_summary(posts, sources):
    now = timezone.now()
    post_last_seen = []
    source_last_seen = []

    for post in posts:
        ts = cache.get(f'analytics:clicks:post:{post.id}:last_seen')
        if ts:
            post_last_seen.append(ts)

    for source in sources:
        ts = cache.get(f'analytics:clicks:source:{source.id}:last_seen')
        if ts:
            source_last_seen.append(ts)

    all_last_seen = post_last_seen + source_last_seen
    oldest_days = None
    newest_days = None
    if all_last_seen:
        oldest_days = round((now - min(all_last_seen)).total_seconds() / 86400.0, 2)
        newest_days = round((now - max(all_last_seen)).total_seconds() / 86400.0, 2)

    return {
        'tracked_last_seen_events': len(all_last_seen),
        'oldest_signal_age_days': oldest_days,
        'newest_signal_age_days': newest_days,
    }


def _monitoring_snapshot(task_name):
    prefix = f'monitoring:task:{task_name}'
    return {
        'task': task_name,
        'last_status': cache.get(f'{prefix}:last_status') or 'never',
        'last_run_at': cache.get(f'{prefix}:last_run_at'),
        'last_success_at': cache.get(f'{prefix}:last_success_at'),
        'last_failure_at': cache.get(f'{prefix}:last_failure_at'),
        'last_error': cache.get(f'{prefix}:last_error') or '',
        'total_runs': int(cache.get(f'{prefix}:total_runs', 0)),
        'total_failures': int(cache.get(f'{prefix}:total_failures', 0)),
        'total_retries': int(cache.get(f'{prefix}:total_retries', 0)),
        'consecutive_failures': int(cache.get(f'{prefix}:consecutive_failures', 0)),
    }


def _monitoring_overview():
    snapshots = [_monitoring_snapshot(task_name) for task_name in MONITORED_TASKS]
    alert_count = sum(
        1
        for item in snapshots
        if item['last_status'] == 'error' or item['consecutive_failures'] > 0
    )
    never_run_count = sum(1 for item in snapshots if item['last_status'] == 'never')
    status = 'degraded' if alert_count else 'healthy'
    return {
        'status': status,
        'alert_count': alert_count,
        'never_run_count': never_run_count,
        'tasks': snapshots,
    }


def _safe_int(value, default, minimum=1, maximum=200):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def digest_posts_queryset(hours=48, limit=8):
    cutoff = timezone.now() - timedelta(hours=hours)
    posts = list(
        Post.published.filter(publish__gte=cutoff)
        .select_related('source_article__source')
        .annotate(
            like_count=Count('likes', distinct=True),
            comment_count=Count('comments', filter=Q(comments__approved=True), distinct=True),
        )
        .order_by('-publish')[:30]
    )
    return _rank_homepage_posts(posts)[:limit]

class PostListView(ListView):
    tag = None
    queryset = Post.published.all()
    context_object_name = "posts"
    paginate_by = 6
    template_name = "blog/post/list.html"

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related('author', 'category', 'source_article__source')
            .prefetch_related('tags')
        )
        self.tag = None
        tag_slug = self.kwargs.get('tag_slug')
        search_query = (self.request.GET.get('q') or '').strip()
        self.search_query = search_query or None
        if tag_slug:
            self.tag = get_object_or_404(Tag, slug=tag_slug)
            queryset = queryset.filter(tags__in=[self.tag])

        if self.search_query:
            queryset = queryset.filter(
                Q(title__icontains=self.search_query)
                | Q(body__icontains=self.search_query)
                | Q(tags__name__icontains=self.search_query)
                | Q(category__name__icontains=self.search_query)
            ).annotate(
                relevance=Case(
                    When(title__iexact=self.search_query, then=Value(100)),
                    When(title__icontains=self.search_query, then=Value(80)),
                    When(body__icontains=self.search_query, then=Value(50)),
                    When(tags__name__icontains=self.search_query, then=Value(40)),
                    When(category__name__icontains=self.search_query, then=Value(30)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ).order_by('-relevance', '-publish')
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tag'] = self.tag
        context['query'] = self.search_query
        context['canonical_url'] = self.request.build_absolute_uri()
        if self.tag:
            context['social_image_url'] = _tag_social_image_url(self.request, self.tag)

        page_obj = context.get('page_obj')
        page_number = page_obj.number if page_obj else 1
        current_page_post_count = len(context.get('posts') or [])
        context['show_in_feed_ad'] = (
            not self.tag
            and not self.search_query
            and page_number % 2 == 1
            and current_page_post_count >= 4
        )

        if not self.tag and not self.search_query:
            base = Post.published.all()
            trending_candidates = list(
                base.select_related('author', 'category', 'source_article__source')
                .prefetch_related('tags')
                .annotate(
                    like_count=Count('likes', distinct=True),
                    comment_count=Count('comments', filter=Q(comments__approved=True), distinct=True),
                )
                .order_by('-like_count', '-comment_count', '-publish')[:12]
            )
            trending_posts = _rank_homepage_posts(trending_candidates)[:4]
            fresh_auto_posts = (
                base.filter(auto_generated=True)
                .select_related('author', 'category', 'source_article__source')
                .prefetch_related('tags')
                .order_by('-publish')[:4]
            )
            editor_posts = (
                base.filter(auto_generated=False)
                .select_related('author', 'category', 'source_article__source')
                .prefetch_related('tags')
                .order_by('-publish')[:4]
            )
            context['trending_posts'] = list(trending_posts)
            context['fresh_auto_posts'] = list(fresh_auto_posts)
            context['editor_posts'] = list(editor_posts)

        if self.search_query:
            context['search_mode'] = True

        return context

def post_detail(request, year, month, day, post):
    post = get_object_or_404(
        Post,
        publish__year=year,
        publish__month=month,
        publish__day=day,
        slug=post,
        status=Post.Status.PUBLISHED
    )
    
    # List of active comments for this post
    comments = post.comments.filter(approved=True)
    
    # Form for users to comment
    form = CommentForm()

    # List of similar posts with weighted relevance.
    post_tags_ids = list(post.tags.values_list('id', flat=True))
    same_source_annotation = Value(0, output_field=IntegerField())
    if post.source_article_id and post.source_article and post.source_article.source_id:
        same_source_annotation = Case(
            When(source_article__source_id=post.source_article.source_id, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )

    similar_posts_qs = (
        Post.published.filter(tags__in=post_tags_ids)
        .exclude(id=post.id)
        .select_related('source_article__source')
        .annotate(
            same_tags=Count('tags', filter=Q(tags__in=post_tags_ids), distinct=True),
            like_count=Count('likes', distinct=True),
            comment_count=Count('comments', filter=Q(comments__approved=True), distinct=True),
            same_source=same_source_annotation,
        )
        .annotate(
            relevance_score=ExpressionWrapper(
                F('same_tags') * Value(30)
                + F('same_source') * Value(20)
                + F('like_count') * Value(3)
                + F('comment_count') * Value(2),
                output_field=IntegerField(),
            )
        )
        .annotate(
            freshness_score=Case(
                When(publish__gte=timezone.now() - timedelta(days=7), then=Value(10)),
                When(publish__gte=timezone.now() - timedelta(days=30), then=Value(5)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .annotate(
            total_score=ExpressionWrapper(
                F('relevance_score') + F('freshness_score'),
                output_field=IntegerField(),
            )
        )
        .order_by('-total_score', '-publish')
    )
    similar_posts = _enforce_source_diversity(list(similar_posts_qs[:12]), max_per_source=2, limit=4)
    
    is_liked = False
    is_bookmarked = False
    if request.user.is_authenticated:
        is_liked = Like.objects.filter(post=post, user=request.user).exists()
        is_bookmarked = Bookmark.objects.filter(post=post, user=request.user).exists()
        
    return render(request, "blog/post/detail.html", {
        "post": post,
        "comments": comments,
        "form": form,
        "similar_posts": similar_posts,
        "is_liked": is_liked,
        "is_bookmarked": is_bookmarked,
        "canonical_url": request.build_absolute_uri(post.get_absolute_url()),
        "social_image_url": _social_image_url(request, post),
    })

def post_share(request, post_id):
    post = get_object_or_404(
        Post,
        id=post_id,
        status=Post.Status.PUBLISHED
    )
    sent = False

    if request.method == "POST":
        form = EmailPostForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            post_url = request.build_absolute_uri(post.get_absolute_url())
            subject = f"{cd['name']} recommends you read {post.title}"
            message = f"Read {post.title} at {post_url}\n\n" \
                      f"{cd['name']}\'s comments: {cd['comments']}"
            from django.core.mail import send_mail
            send_mail(subject, message, 'admin@myblog.com', [cd['to']])
            sent = True
    else:
        form = EmailPostForm()
    return render(
        request,
        "blog/post/share.html",
        {
            "post": post,
            "form": form,
            "sent": sent,
            "canonical_url": request.build_absolute_uri(post.get_absolute_url()),
        }
    )

@require_POST
@login_required
def post_comment(request, post_id):
    post = get_object_or_404(
        Post,
        id=post_id,
        status=Post.Status.PUBLISHED
    )
    form = CommentForm(request.POST)
    if form.is_valid():
        comment = form.save(commit=False)
        comment.post = post
        comment.user = request.user
        comment.save()
        return redirect(post.get_absolute_url())
    
    return render(request, "blog/post/detail.html", {
        "post": post,
        "form": form
    })

@require_POST
@login_required
def post_like(request, post_id):
    post = get_object_or_404(Post, id=post_id, status=Post.Status.PUBLISHED)
    like = Like.objects.filter(post=post, user=request.user)
    is_liked = False
    if like.exists():
        like.delete()
        is_liked = False
    else:
        Like.objects.create(post=post, user=request.user)
        is_liked = True
    
    return JsonResponse({
        'liked': is_liked,
        'count': post.likes.count()
    })


@require_POST
@login_required
def post_bookmark(request, post_id):
    post = get_object_or_404(Post, id=post_id, status=Post.Status.PUBLISHED)
    bookmark = Bookmark.objects.filter(post=post, user=request.user)
    is_bookmarked = False
    if bookmark.exists():
        bookmark.delete()
        is_bookmarked = False
    else:
        Bookmark.objects.create(post=post, user=request.user)
        is_bookmarked = True

    return JsonResponse(
        {
            'bookmarked': is_bookmarked,
            'count': post.bookmarks.count(),
        }
    )


@login_required
@require_GET
def bookmarks_list(request):
    posts = (
        Post.published.filter(bookmarks__user=request.user)
        .select_related('author', 'category', 'source_article__source')
        .prefetch_related('tags')
        .order_by('-bookmarks__created')
        .distinct()
    )
    return render(
        request,
        'blog/bookmarks/list.html',
        {
            'posts': posts,
            'canonical_url': request.build_absolute_uri(),
        },
    )


@require_GET
def sports_hub(request):
    active_tab = (request.GET.get('tab') or 'news').strip().lower()
    if active_tab not in {'news', 'fixtures', 'tables'}:
        active_tab = 'news'

    base = (
        Post.published.select_related('author', 'category', 'source_article__source')
        .prefetch_related('tags')
        .annotate(
            like_count=Count('likes', distinct=True),
            comment_count=Count('comments', filter=Q(comments__approved=True), distinct=True),
        )
    )
    sports_posts = list(
        base.filter(
            Q(source_article__source__provider=NewsSource.Provider.OPENLIGADB)
        )
        .order_by('-publish')[:24]
    )

    fixtures_by_league = []
    tables_by_league = []
    for league in OPENLIGA_MAIN_LEAGUES:
        code = league['code']
        fixtures_raw = _fetch_openligadb_endpoint(f'getmatchdata/{code}')
        tables_raw = _fetch_openligadb_endpoint(f'getbltable/{code}')

        fixtures = []
        for item in (fixtures_raw or [])[:10]:
            team1 = ((item.get('Team1') or {}).get('TeamName') or 'Home').strip()
            team2 = ((item.get('Team2') or {}).get('TeamName') or 'Away').strip()
            kickoff = (item.get('MatchDateTime') or item.get('MatchDateTimeUTC') or '').strip()
            result_block = (item.get('MatchResults') or [])
            final_score = result_block[-1] if result_block else {}
            score = ''
            if final_score.get('PointsTeam1') is not None and final_score.get('PointsTeam2') is not None:
                score = f"{final_score.get('PointsTeam1')} - {final_score.get('PointsTeam2')}"
            fixtures.append({'home': team1, 'away': team2, 'kickoff': kickoff, 'score': score})

        standings = []
        for row in (tables_raw or [])[:12]:
            standings.append(
                {
                    'rank': row.get('platz') or row.get('rank') or row.get('position') or '',
                    'team': row.get('teamName') or row.get('team') or '',
                    'points': row.get('points') or row.get('punkte') or 0,
                    'goals': row.get('goals') or row.get('tore') or 0,
                    'conceded': row.get('opponentGoals') or row.get('gegentore') or 0,
                    'matches': row.get('matches') or row.get('spiele') or 0,
                }
            )

        fixtures_by_league.append({'league': league['name'], 'code': code, 'fixtures': fixtures})
        tables_by_league.append({'league': league['name'], 'code': code, 'rows': standings})

    return render(
        request,
        'blog/sports/hub.html',
        {
            'active_tab': active_tab,
            'sports_news': _rank_homepage_posts(sports_posts)[:12],
            'fixtures_by_league': fixtures_by_league,
            'tables_by_league': tables_by_league,
            'canonical_url': request.build_absolute_uri(),
        },
    )
@require_POST
def newsletter_subscribe(request):
    form = NewsletterSubscriptionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({'subscribed': False, 'reason': 'invalid_email'}, status=400)

    email = form.cleaned_data['email'].strip().lower()
    subscriber, _ = NewsletterSubscriber.objects.get_or_create(email=email)
    if not subscriber.is_active:
        subscriber.is_active = True
        subscriber.save(update_fields=['is_active', 'updated'])

    return JsonResponse({'subscribed': True, 'email': email})


@require_POST
@csrf_exempt
def track_post_click(request):
    post_id = request.POST.get('post_id')
    placement = (request.POST.get('placement') or 'unknown').strip().lower()[:32]

    if not post_id or not str(post_id).isdigit():
        return JsonResponse({'tracked': False, 'reason': 'invalid_post_id'}, status=400)

    post_id = int(post_id)
    if not Post.published.filter(id=post_id).exists():
        return JsonResponse({'tracked': False, 'reason': 'not_found'}, status=404)

    post = Post.published.select_related('source_article__source').get(id=post_id)
    source_id = post.source_article.source_id if post.source_article_id and post.source_article else None

    total_key = f'analytics:clicks:post:{post_id}:total'
    placement_key = f'analytics:clicks:post:{post_id}:placement:{placement}'
    source_total_key = f'analytics:clicks:source:{source_id}:total' if source_id else None
    source_placement_key = f'analytics:clicks:source:{source_id}:placement:{placement}' if source_id else None
    ttl_seconds = _analytics_retention_seconds()
    total_count = cache.get(total_key, 0) + 1
    placement_count = cache.get(placement_key, 0) + 1
    cache.set(total_key, total_count, timeout=ttl_seconds)
    cache.set(placement_key, placement_count, timeout=ttl_seconds)
    source_total_count = None
    if source_total_key:
        source_total_count = cache.get(source_total_key, 0) + 1
        cache.set(source_total_key, source_total_count, timeout=ttl_seconds)
        cache.set(f'analytics:clicks:source:{source_id}:last_seen', timezone.now(), timeout=ttl_seconds)
    cache.set(f'analytics:clicks:post:{post_id}:last_seen', timezone.now(), timeout=ttl_seconds)
    if source_placement_key:
        source_placement_count = cache.get(source_placement_key, 0) + 1
        cache.set(source_placement_key, source_placement_count, timeout=ttl_seconds)

    return JsonResponse(
        {
            'tracked': True,
            'post_id': post_id,
            'source_id': source_id,
            'placement': placement,
            'total_clicks': int(total_count),
            'source_clicks': int(source_total_count) if source_total_count is not None else None,
        }
    )


@require_GET
def post_social_image(request, year, month, day, post):
    post_obj = get_object_or_404(
        Post,
        publish__year=year,
        publish__month=month,
        publish__day=day,
        slug=post,
        status=Post.Status.PUBLISHED,
    )
    source_name = post_obj.source_article.source.name if post_obj.source_article_id and post_obj.source_article and post_obj.source_article.source else 'Stunning Blog'
    body = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-labelledby="title desc">
  <title id="title">{escape(post_obj.title)}</title>
  <desc id="desc">{escape(source_name)} story image for social sharing and sitemap coverage.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="60%" stop-color="#1e1b4b"/>
      <stop offset="100%" stop-color="#831843"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>
  <circle cx="180" cy="120" r="120" fill="#38bdf8" opacity="0.2"/>
  <circle cx="1030" cy="520" r="180" fill="#f472b6" opacity="0.2"/>
  <text x="80" y="280" font-family="Segoe UI, Arial, sans-serif" font-size="66" font-weight="700" fill="#ffffff">{escape(post_obj.title)}</text>
  <text x="80" y="365" font-family="Segoe UI, Arial, sans-serif" font-size="32" fill="#e2e8f0">{escape(source_name)}</text>
  <text x="80" y="430" font-family="Segoe UI, Arial, sans-serif" font-size="26" fill="#cbd5e1">Curated stories and AI-assisted summaries</text>
</svg>'''
    return HttpResponse(body, content_type='image/svg+xml')


@require_GET
def tag_social_image(request, tag_slug):
    tag = get_object_or_404(Tag, slug=tag_slug)
    tagged_posts = list(Post.published.filter(tags__slug=tag.slug).select_related('source_article__source').order_by('-publish')[:3])
    top_source_name = tagged_posts[0].source_article.source.name if tagged_posts and tagged_posts[0].source_article_id and tagged_posts[0].source_article and tagged_posts[0].source_article.source else 'Stunning Blog'
    sample_title = tagged_posts[0].title if tagged_posts else 'Curated stories and summaries'
    body = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630" role="img" aria-labelledby="title desc">
    <title id="title">Tag archive for {escape(tag.name)}</title>
    <desc id="desc">Social preview image for the {escape(tag.name)} tag archive.</desc>
    <defs>
        <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#0f172a"/>
            <stop offset="60%" stop-color="#312e81"/>
            <stop offset="100%" stop-color="#831843"/>
        </linearGradient>
    </defs>
    <rect width="1200" height="630" fill="url(#bg)"/>
    <circle cx="180" cy="120" r="120" fill="#38bdf8" opacity="0.18"/>
    <circle cx="1020" cy="500" r="180" fill="#f472b6" opacity="0.18"/>
    <text x="80" y="240" font-family="Segoe UI, Arial, sans-serif" font-size="32" fill="#cbd5e1">Tag archive</text>
    <text x="80" y="330" font-family="Segoe UI, Arial, sans-serif" font-size="78" font-weight="700" fill="#ffffff">#{escape(tag.name)}</text>
    <text x="80" y="405" font-family="Segoe UI, Arial, sans-serif" font-size="30" fill="#e2e8f0">{escape(sample_title)}</text>
    <text x="80" y="460" font-family="Segoe UI, Arial, sans-serif" font-size="24" fill="#cbd5e1">Top source: {escape(top_source_name)}</text>
</svg>'''
    return HttpResponse(body, content_type='image/svg+xml')


@staff_member_required
@require_GET
def analytics_dashboard(request):
    posts = list(Post.published.select_related('source_article__source').order_by('-publish'))
    sources = list(NewsSource.objects.filter(is_active=True).order_by('name'))
    retention_days = max(1, int(getattr(settings, 'ANALYTICS_RETENTION_DAYS', 30)))
    retention_summary = _retention_summary(posts, sources)
    monitoring_overview = _monitoring_overview()

    top_posts = sorted(
        (
            {
                'post': post,
                'clicks': _cached_click_count('analytics:clicks:post', post.id),
                'source_clicks': _cached_click_count(
                    'analytics:clicks:source',
                    post.source_article.source_id if post.source_article_id and post.source_article and post.source_article.source_id else 0,
                ),
            }
            for post in posts
        ),
        key=lambda item: (item['clicks'], item['source_clicks'], item['post'].publish),
        reverse=True,
    )[:10]

    top_sources = sorted(
        (
            {
                'source': source,
                'clicks': _cached_click_count('analytics:clicks:source', source.id),
            }
            for source in sources
        ),
        key=lambda item: (item['clicks'], item['source'].trust_score, item['source'].updated),
        reverse=True,
    )[:10]

    return render(
        request,
        'blog/analytics/dashboard.html',
        {
            'canonical_url': request.build_absolute_uri(),
            'top_posts': top_posts,
            'top_sources': top_sources,
            'tracked_posts_count': len(posts),
            'tracked_sources_count': len(sources),
            'total_clicks': sum(item['clicks'] for item in top_posts),
            'analytics_retention_days': retention_days,
            'retention_summary': retention_summary,
            'monitoring_overview': monitoring_overview,
            'manual_ops_result': request.session.pop('manual_ops_result', None),
        },
    )


@staff_member_required
@require_POST
def run_manual_pipeline(request):
    action = (request.POST.get('action') or 'full').strip().lower()
    fetch_limit = _safe_int(request.POST.get('fetch_limit'), default=20, minimum=1, maximum=100)
    summarize_limit = _safe_int(request.POST.get('summarize_limit'), default=50, minimum=1, maximum=200)
    publish_limit = _safe_int(request.POST.get('publish_limit'), default=50, minimum=1, maximum=200)

    result = {
        'action': action,
        'fetch_limit': fetch_limit,
        'summarize_limit': summarize_limit,
        'publish_limit': publish_limit,
    }

    valid_actions = {'fetch', 'summarize', 'publish', 'full'}
    if action not in valid_actions:
        result['status'] = 'error'
        result['error'] = 'invalid_action'
        request.session['manual_ops_result'] = result
        return redirect('blog:analytics_dashboard')

    try:
        if action in {'fetch', 'full'}:
            result['fetch'] = fetch_all_active_sources(max_items=fetch_limit)
        if action in {'summarize', 'full'}:
            result['summarize'] = summarize_pending_articles(limit=summarize_limit)
        if action in {'publish', 'full'}:
            result['publish'] = auto_publish_trusted_articles(limit=publish_limit)
        result['status'] = 'ok'
    except Exception as exc:
        result['status'] = 'error'
        result['error'] = str(exc)[:400]

    request.session['manual_ops_result'] = result
    return redirect('blog:analytics_dashboard')


@staff_member_required
@require_GET
def monitoring_health(request):
    overview = _monitoring_overview()
    return JsonResponse(
        {
            'generated_at': timezone.now().isoformat(),
            'status': overview['status'],
            'alert_count': overview['alert_count'],
            'never_run_count': overview['never_run_count'],
            'tasks': overview['tasks'],
        }
    )


@staff_member_required
@require_GET
def launch_readiness_health(request):
    report = compute_launch_readiness_checks()
    status = 'ready' if report['fail_count'] == 0 else 'needs_attention'
    return JsonResponse(
        {
            'generated_at': timezone.now().isoformat(),
            'status': status,
            'pass_count': report['pass_count'],
            'fail_count': report['fail_count'],
            'total_count': report['total_count'],
            'checks': report['checks'],
        }
    )


@staff_member_required
@require_GET
def analytics_export_csv(request):
    posts = list(Post.published.select_related('source_article__source').order_by('-publish'))
    sources = list(NewsSource.objects.filter(is_active=True).order_by('name'))

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="analytics-dashboard.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'type',
        'id',
        'title_or_name',
        'clicks',
        'source_clicks',
        'publish_or_updated',
        'meta',
    ])

    for post in posts:
        source_id = post.source_article.source_id if post.source_article_id and post.source_article and post.source_article.source_id else None
        writer.writerow([
            'post',
            post.id,
            post.title,
            _cached_click_count('analytics:clicks:post', post.id),
            _cached_click_count('analytics:clicks:source', source_id) if source_id else 0,
            post.publish.isoformat(),
            post.source_article.source.name if post.source_article_id and post.source_article and post.source_article.source else '',
        ])

    for source in sources:
        writer.writerow([
            'source',
            source.id,
            source.name,
            _cached_click_count('analytics:clicks:source', source.id),
            '',
            source.updated.isoformat(),
            source.get_provider_display(),
        ])

    return response


@staff_member_required
@require_GET
def analytics_export_trending_snapshot(request):
    posts = list(
        Post.published.select_related('source_article__source')
        .prefetch_related('tags')
        .annotate(
            like_count=Count('likes', distinct=True),
            comment_count=Count('comments', filter=Q(comments__approved=True), distinct=True),
        )
        .order_by('-publish')[:20]
    )

    ranked = _rank_homepage_posts(posts)[:10]

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="trending-snapshot.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'rank',
        'post_id',
        'title',
        'score',
        'likes',
        'comments',
        'post_clicks',
        'source_clicks',
        'freshness_bonus',
        'source_name',
        'publish_date',
    ])

    for index, post in enumerate(ranked, start=1):
        metrics = _home_feed_score(post)
        writer.writerow([
            index,
            post.id,
            post.title,
            round(metrics['total'], 2),
            metrics['like_count'],
            metrics['comment_count'],
            round(metrics['post_clicks'], 2),
            round(metrics['source_clicks'], 2),
            metrics['freshness'],
            post.source_article.source.name if post.source_article_id and post.source_article and post.source_article.source else '',
            post.publish.date().isoformat(),
        ])

    return response


@staff_member_required
@require_POST
def analytics_reset_all(request):
    confirm = (request.POST.get('confirm') or '').strip().lower()
    if confirm != 'yes':
        return JsonResponse(
            {
                'reset': False,
                'reason': 'confirmation_required',
            },
            status=400,
        )

    _clear_all_analytics_metrics()
    return JsonResponse(
        {
            'reset': True,
            'message': 'All analytics cache entries were cleared.',
        }
    )


@require_GET
def legal_page(request, page):
    pages = {
        'privacy': {
            'title': 'Privacy Policy',
            'body': 'We collect minimal data required to operate this blog, deliver content, and improve quality. Third-party providers like analytics and ad networks may process anonymized usage data.',
        },
        'about': {
            'title': 'About',
            'body': 'This platform publishes curated and summarized news content from trusted sources, with quality checks, attribution, and moderation workflows to ensure reader value.',
        },
        'disclaimer': {
            'title': 'Disclaimer',
            'body': 'Content may include AI-assisted summaries for informational purposes. Always verify critical information with primary sources before making decisions.',
        },
        'contact': {
            'title': 'Contact',
            'body': 'For support, corrections, or business inquiries, contact us via the official email address shown in the website footer.',
        },
    }
    if page not in pages:
        return HttpResponse(status=404)

    context = {
        'page_title': pages[page]['title'],
        'page_body': pages[page]['body'],
        'canonical_url': request.build_absolute_uri(),
    }
    return render(request, 'blog/legal/page.html', context)


@require_GET
def robots_txt(request):
    sitemap_url = request.build_absolute_uri(reverse('blog:sitemap_xml'))
    content = (
        'User-agent: *\n'
        'Allow: /\n\n'
        f'Sitemap: {sitemap_url}\n'
    )
    return HttpResponse(content, content_type='text/plain')


@require_GET
def ads_txt(request):
    adsense_client_id = getattr(settings, 'ADSENSE_CLIENT_ID', '').strip()
    publisher_id = adsense_client_id.replace('ca-pub-', 'pub-') if adsense_client_id else ''
    content = ''
    if publisher_id:
        content = f'google.com, {publisher_id}, DIRECT, f08c47fec0942fa0\n'
    return HttpResponse(content, content_type='text/plain')


@require_GET
def sitemap_xml(request):
    static_urls = [
        reverse('blog:post_list'),
        reverse('blog:sports_hub'),
        reverse('blog:legal_page', kwargs={'page': 'privacy'}),
        reverse('blog:legal_page', kwargs={'page': 'about'}),
        reverse('blog:legal_page', kwargs={'page': 'disclaimer'}),
        reverse('blog:legal_page', kwargs={'page': 'contact'}),
    ]
    post_urls = list(
        Post.published.values_list(
            'publish',
            'updated',
            'slug',
            'id',
            'auto_generated',
            'title',
            'source_article__source__name',
            named=True,
        )
    )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">',
    ]

    for path in static_urls:
        loc = request.build_absolute_uri(path)
        lines.append('<url>')
        lines.append(f'<loc>{loc}</loc>')
        if path == reverse('blog:post_list'):
            lines.append('<changefreq>hourly</changefreq>')
            lines.append('<priority>1.0</priority>')
        else:
            lines.append('<changefreq>monthly</changefreq>')
            lines.append('<priority>0.4</priority>')
        lines.append('</url>')

    for post in post_urls:
        post_path = reverse(
            'blog:post_detail',
            kwargs={
                'year': post.publish.year,
                'month': post.publish.month,
                'day': post.publish.day,
                'post': post.slug,
            },
        )
        loc = request.build_absolute_uri(post_path)
        lastmod_dt = post.updated if post.updated and post.updated > post.publish else post.publish
        lastmod = lastmod_dt.strftime('%Y-%m-%d')
        publish_iso = post.publish.strftime('%Y-%m-%dT%H:%M:%S+00:00')
        source_name = post.source_article__source__name or 'Stunning Blog'

        lines.append('<url>')
        lines.append(f'<loc>{loc}</loc>')
        lines.append(f'<lastmod>{lastmod}</lastmod>')
        lines.append('<changefreq>weekly</changefreq>')
        lines.append(f'<priority>{"0.8" if post.auto_generated else "0.9"}</priority>')
        lines.append('<image:image>')
        lines.append(f'<image:loc>{request.build_absolute_uri(reverse("blog:post_social_image", kwargs={"year": post.publish.year, "month": post.publish.month, "day": post.publish.day, "post": post.slug}))}</image:loc>')
        lines.append(f'<image:title>{escape(post.title)}</image:title>')
        lines.append(f'<image:caption>{escape(post.source_article__source__name or "Stunning Blog")}</image:caption>')
        lines.append('</image:image>')

        if post.publish >= timezone.now() - timedelta(days=2):
            lines.append('<news:news>')
            lines.append('<news:publication>')
            lines.append(f'<news:name>{escape(source_name)}</news:name>')
            lines.append('<news:language>en</news:language>')
            lines.append('</news:publication>')
            lines.append(f'<news:publication_date>{publish_iso}</news:publication_date>')
            lines.append(f'<news:title>{escape(post.title)}</news:title>')
            lines.append('</news:news>')

        lines.append('</url>')

    lines.append('</urlset>')
    return HttpResponse('\n'.join(lines), content_type='application/xml')
