from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.core.cache import cache
from django.utils import timezone
from django.utils.text import slugify
from datetime import timedelta
import time

from blog.models import Article, NewsSource, Post
from blog.celery_compat import shared_task
from blog.services import ArticleSummarizationService, NewsIngestionService


def _monitoring_retention_seconds() -> int:
    days = max(1, int(getattr(settings, 'MONITORING_RETENTION_DAYS', 30)))
    return days * 24 * 60 * 60


def _monitoring_key(task_name: str, metric: str) -> str:
    return f'monitoring:task:{task_name}:{metric}'


def _record_task_start(task_name: str) -> None:
    ttl = _monitoring_retention_seconds()
    now = timezone.now()
    total_runs_key = _monitoring_key(task_name, 'total_runs')
    total_runs = int(cache.get(total_runs_key, 0)) + 1
    cache.set(total_runs_key, total_runs, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_run_at'), now, timeout=ttl)


def _record_task_success(task_name: str) -> None:
    ttl = _monitoring_retention_seconds()
    now = timezone.now()
    cache.set(_monitoring_key(task_name, 'last_status'), 'ok', timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_success_at'), now, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'consecutive_failures'), 0, timeout=ttl)


def _record_task_failure(task_name: str, exc: Exception) -> None:
    ttl = _monitoring_retention_seconds()
    now = timezone.now()
    total_failures_key = _monitoring_key(task_name, 'total_failures')
    consecutive_key = _monitoring_key(task_name, 'consecutive_failures')
    total_failures = int(cache.get(total_failures_key, 0)) + 1
    consecutive_failures = int(cache.get(consecutive_key, 0)) + 1
    cache.set(total_failures_key, total_failures, timeout=ttl)
    cache.set(consecutive_key, consecutive_failures, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_status'), 'error', timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_failure_at'), now, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_error'), str(exc)[:300], timeout=ttl)


def _retry_tuning():
    max_attempts = max(1, int(getattr(settings, 'TASK_RETRY_MAX_ATTEMPTS', 3)))
    backoff_base = max(1, int(getattr(settings, 'TASK_RETRY_BACKOFF_BASE_SECONDS', 2)))
    backoff_max = max(backoff_base, int(getattr(settings, 'TASK_RETRY_BACKOFF_MAX_SECONDS', 30)))
    apply_sleep = bool(getattr(settings, 'TASK_RETRY_APPLY_SLEEP', False))
    return max_attempts, backoff_base, backoff_max, apply_sleep


def _retry_delay_seconds(attempt_number: int) -> int:
    _, backoff_base, backoff_max, _ = _retry_tuning()
    delay = backoff_base * (2 ** max(0, attempt_number - 1))
    return min(backoff_max, delay)


def _record_task_retry(task_name: str, attempt_number: int, delay_seconds: int, exc: Exception) -> None:
    ttl = _monitoring_retention_seconds()
    retries_key = _monitoring_key(task_name, 'total_retries')
    total_retries = int(cache.get(retries_key, 0)) + 1
    cache.set(retries_key, total_retries, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_retry_attempt'), attempt_number, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_retry_delay_seconds'), delay_seconds, timeout=ttl)
    cache.set(_monitoring_key(task_name, 'last_retry_error'), str(exc)[:300], timeout=ttl)


def _execute_with_retry(task_name: str, operation, non_retry_exceptions=()):
    max_attempts, _, _, apply_sleep = _retry_tuning()
    attempt = 1

    while True:
        try:
            return operation()
        except non_retry_exceptions:
            raise
        except Exception as exc:
            if attempt >= max_attempts:
                raise

            delay_seconds = _retry_delay_seconds(attempt)
            _record_task_retry(task_name, attempt, delay_seconds, exc)
            if apply_sleep:
                time.sleep(delay_seconds)
            attempt += 1


def _build_unique_slug(title: str, publish_dt):
    base = slugify(title)[:240] or "article"
    slug = base
    counter = 1
    while Post.objects.filter(slug=slug, publish__date=publish_dt.date()).exists():
        suffix = f"-{counter}"
        slug = f"{base[:255 - len(suffix)]}{suffix}"
        counter += 1
    return slug


def _qualifies_for_auto_publish(article: Article) -> bool:
    min_trust = getattr(settings, "AUTO_PUBLISH_MIN_TRUST_SCORE", 70)
    require_ad_safe = getattr(settings, "AUTO_PUBLISH_REQUIRE_AD_SAFE", True)
    min_originality = getattr(settings, "AUTO_PUBLISH_MIN_ORIGINALITY", 40)

    if not article.source.auto_publish:
        return False
    if article.source.trust_score < min_trust:
        return False
    if article.originality_score < min_originality:
        return False
    if require_ad_safe and not article.is_ad_safe:
        return False
    if article.source.provider == NewsSource.Provider.TELEGRAM:
        if getattr(settings, 'TELEGRAM_REQUIRE_MANUAL_REVIEW', True):
            return False
        if not getattr(settings, 'FEATURE_FLAG_TELEGRAM_AUTOPUBLISH_ENABLED', False):
            return False
    if article.status != Article.Status.SUMMARIZED:
        return False
    if Post.objects.filter(source_article=article).exists():
        return False
    return True


@shared_task
def fetch_source_articles(source_id: int, max_items: int = 20) -> dict:
    task_name = 'fetch_source_articles'
    _record_task_start(task_name)
    try:
        if not getattr(settings, 'FEATURE_FLAG_INGESTION_ENABLED', True):
            payload = {
                'status': 'skipped',
                'reason': 'feature_disabled',
                'source_id': int(source_id),
            }
            _record_task_success(task_name)
            return payload

        source = NewsSource.objects.get(id=source_id, is_active=True)
        effective_max_items = max_items
        if source.provider == NewsSource.Provider.TELEGRAM:
            if not getattr(settings, 'FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED', False):
                payload = {
                    'source_id': int(source.pk),
                    'source_name': source.name,
                    'status': 'skipped',
                    'reason': 'telegram_feature_disabled',
                }
                _record_task_success(task_name)
                return payload

            schedule_minutes = max(1, int(getattr(settings, 'TELEGRAM_FETCH_INTERVAL_MINUTES', 120)))
            schedule_key = f'monitoring:telegram:source:{source.pk}:last_fetch_at'
            last_fetch_at = cache.get(schedule_key)
            if last_fetch_at and timezone.now() - last_fetch_at < timedelta(minutes=schedule_minutes):
                payload = {
                    'source_id': int(source.pk),
                    'source_name': source.name,
                    'status': 'skipped',
                    'reason': 'telegram_schedule_window',
                }
                _record_task_success(task_name)
                return payload

            telegram_limit = max(1, int(getattr(settings, 'TELEGRAM_FETCH_MAX_ITEMS', 10)))
            effective_max_items = min(max_items, telegram_limit)

        def operation():
            service = NewsIngestionService()
            return service.fetch_and_store(source=source, max_items=effective_max_items)

        result = _execute_with_retry(
            task_name,
            operation,
            non_retry_exceptions=(ObjectDoesNotExist,),
        )
        payload = {
            'source_id': int(source.pk),
            'source_name': result.source_name,
            'status': 'ok',
            'fetched': result.fetched,
            'created': result.created,
            'updated': result.updated,
        }
        if source.provider == NewsSource.Provider.TELEGRAM:
            cache.set(
                f'monitoring:telegram:source:{source.pk}:last_fetch_at',
                timezone.now(),
                timeout=_monitoring_retention_seconds(),
            )
        _record_task_success(task_name)
        return payload
    except Exception as exc:
        _record_task_failure(task_name, exc)
        raise


@shared_task
def fetch_all_active_sources(max_items: int = 20) -> dict:
    task_name = 'fetch_all_active_sources'
    _record_task_start(task_name)
    try:
        if not getattr(settings, 'FEATURE_FLAG_INGESTION_ENABLED', True):
            payload = {'status': 'skipped', 'reason': 'feature_disabled', 'sources': 0, 'results': []}
            _record_task_success(task_name)
            return payload

        source_ids = list(
            NewsSource.objects.filter(is_active=True)
            .values_list('id', flat=True)
        )

        if not source_ids:
            payload = {'status': 'ok', 'sources': 0, 'results': []}
            _record_task_success(task_name)
            return payload

        results = []
        for source_id in source_ids:
            results.append(fetch_source_articles(source_id=source_id, max_items=max_items))

        payload = {'status': 'ok', 'sources': len(source_ids), 'results': results}
        _record_task_success(task_name)
        return payload
    except Exception as exc:
        _record_task_failure(task_name, exc)
        raise


@shared_task
def summarize_pending_articles(limit: int = 20) -> dict:
    task_name = 'summarize_pending_articles'
    _record_task_start(task_name)
    try:
        if not getattr(settings, 'FEATURE_FLAG_SUMMARIZATION_ENABLED', True):
            payload = {'status': 'skipped', 'reason': 'feature_disabled', 'summarized': 0}
            _record_task_success(task_name)
            return payload

        queryset = Article.objects.filter(status=Article.Status.INGESTED).order_by('-fetched_at')[:limit]
        updated = 0

        for article in queryset:
            def operation(current_article=article):
                summarizer = ArticleSummarizationService()
                return summarizer.summarize_article(current_article)

            _execute_with_retry(task_name, operation)
            updated += 1

        payload = {'status': 'ok', 'summarized': updated}
        _record_task_success(task_name)
        return payload
    except Exception as exc:
        _record_task_failure(task_name, exc)
        raise


@shared_task
def auto_publish_trusted_articles(limit: int = 20) -> dict:
    task_name = 'auto_publish_trusted_articles'
    _record_task_start(task_name)
    User = get_user_model()
    try:
        if not getattr(settings, 'FEATURE_FLAG_AUTOPUBLISH_ENABLED', True):
            payload = {'status': 'skipped', 'reason': 'feature_disabled', 'published': 0, 'reviewed': 0}
            _record_task_success(task_name)
            return payload

        author = User.objects.filter(is_staff=True).order_by('id').first()
        if not author:
            payload = {
                'status': 'error',
                'reason': 'no_staff_author',
                'published': 0,
                'reviewed': 0,
            }
            _record_task_failure(task_name, RuntimeError('no_staff_author'))
            return payload

        candidates = (
            Article.objects.select_related('source')
            .filter(status=Article.Status.SUMMARIZED)
            .order_by('-fetched_at')[:limit]
        )

        published = 0
        reviewed = 0
        for article in candidates:
            if _qualifies_for_auto_publish(article):
                publish_dt = article.published_at or timezone.now()
                Post.objects.create(
                    title=article.title,
                    slug=_build_unique_slug(article.title, publish_dt),
                    author=author,
                    body=article.summary or article.body,
                    summary=article.summary,
                    cover_image_url=article.image_url,
                    publish=publish_dt,
                    status=Post.Status.PUBLISHED,
                    auto_generated=True,
                    source_article=article,
                    category=None,
                )
                article.status = Article.Status.PUBLISHED
                article.save(update_fields=['status', 'updated'])
                published += 1
            else:
                article.status = Article.Status.PENDING_REVIEW
                article.save(update_fields=['status', 'updated'])
                reviewed += 1

        payload = {
            'status': 'ok',
            'published': published,
            'reviewed': reviewed,
        }
        _record_task_success(task_name)
        return payload
    except Exception as exc:
        _record_task_failure(task_name, exc)
        raise


@shared_task
def rollback_auto_published_posts(limit: int = 20) -> dict:
    task_name = 'rollback_auto_published_posts'
    _record_task_start(task_name)
    try:
        if not getattr(settings, 'FEATURE_FLAG_ROLLBACK_ENABLED', True):
            payload = {'status': 'skipped', 'reason': 'feature_disabled', 'rolled_back': 0}
            _record_task_success(task_name)
            return payload

        posts = (
            Post.objects.select_related('source_article')
            .filter(auto_generated=True, source_article__isnull=False, status=Post.Status.PUBLISHED)
            .order_by('-publish')[:limit]
        )

        rolled_back = 0
        for post in posts:
            source_article = post.source_article
            post.status = Post.Status.DRAFT
            post.save(update_fields=['status', 'updated'])

            if source_article and source_article.status == Article.Status.PUBLISHED:
                source_article.status = Article.Status.PENDING_REVIEW
                source_article.save(update_fields=['status', 'updated'])
            rolled_back += 1

        payload = {'status': 'ok', 'rolled_back': rolled_back}
        _record_task_success(task_name)
        return payload
    except Exception as exc:
        _record_task_failure(task_name, exc)
        raise
