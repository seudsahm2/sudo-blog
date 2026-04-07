# blog/admin.py
from django.contrib import admin
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from django.utils.text import slugify

from .models import Article, Bookmark, Category, Comment, Like, NewsSource, NewsletterSubscriber, Post


def _build_unique_slug(title, publish_dt):
    base = slugify(title)[:240] or "article"
    slug = base
    counter = 1
    while Post.objects.filter(slug=slug, publish__date=publish_dt.date()).exists():
        suffix = f"-{counter}"
        slug = f"{base[:255 - len(suffix)]}{suffix}"
        counter += 1
    return slug


def _clear_post_click_metrics(post_id):
    cache.delete(f'analytics:clicks:post:{post_id}:total')
    cache.delete(f'analytics:clicks:post:{post_id}:last_seen')


def _clear_source_click_metrics(source_id):
    cache.delete(f'analytics:clicks:source:{source_id}:total')
    cache.delete(f'analytics:clicks:source:{source_id}:last_seen')

@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ['title', 'slug', 'author', 'publish', 'status', 'auto_generated', 'tracked_clicks']   # columns shown in changelist
    list_filter = ['status', 'auto_generated', 'created', 'publish', 'author']  
    search_fields = ['title', 'body']
    prepopulated_fields = {'slug': ('title',)}
    raw_id_fields = ['author', 'source_article']
    date_hierarchy = 'publish'
    ordering = ['status', 'publish']                                    # for easy many-to-many editing
    actions = ['unpublish_auto_generated_posts', 'reset_click_metrics']
    show_facets = admin.ShowFacets.ALWAYS

    def tracked_clicks(self, obj):
        return cache.get(f'analytics:clicks:post:{obj.pk}:total', 0)

    tracked_clicks.short_description = 'Clicks'

    @admin.action(description='Unpublish selected auto-generated posts')
    def unpublish_auto_generated_posts(self, request, queryset):
        updated_posts = 0
        reviewed_articles = 0

        for post in queryset.select_related('source_article'):
            if not post.auto_generated:
                continue
            if post.status != Post.Status.PUBLISHED:
                continue

            post.status = Post.Status.DRAFT
            post.save(update_fields=['status', 'updated'])
            updated_posts += 1

            source_article = post.source_article
            if source_article and source_article.status == Article.Status.PUBLISHED:
                source_article.status = Article.Status.PENDING_REVIEW
                source_article.save(update_fields=['status', 'updated'])
                reviewed_articles += 1

        self.message_user(
            request,
            f"{updated_posts} auto-generated post(s) unpublished; {reviewed_articles} linked article(s) returned to review.",
        )

    @admin.action(description='Reset click metrics for selected posts')
    def reset_click_metrics(self, request, queryset):
        for post in queryset.select_related('source_article'):
            _clear_post_click_metrics(post.pk)
            if post.source_article_id and post.source_article and post.source_article.source_id:
                _clear_source_click_metrics(post.source_article.source_id)

        self.message_user(request, f"{queryset.count()} post click metric(s) reset.")

# Register Category admin
@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']                                   # show name and slug
    search_fields = ['name']                                           # allow searching by name
    prepopulated_fields = {'slug': ('name',)}                          # auto-fill slug from name
    ordering = ['name']                                                # sort categories alphabetically
    show_facets = admin.ShowFacets.ALWAYS                              # show facets
                            # show facets

# Register Comment admin
@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ['id', 'post', 'user', 'short_body', 'approved', 'created']  # include helper short_body
    list_filter = ['approved', 'created', 'user']                                # filters for moderation
    search_fields = ['body', 'user__username', 'post__title']                     # search across relations
    raw_id_fields = ['post', 'user']                                              # raw id widgets for performance
    date_hierarchy = 'created'                                                    # drill-down by created date
    ordering = ['-created']                                                       # newest comments first
    show_facets = admin.ShowFacets.ALWAYS                                         # show facets

    def short_body(self, obj):                                                    # small helper column to avoid huge text
        return (obj.body[:75] + '...') if obj.body and len(obj.body) > 75 else obj.body
    short_body.short_description = 'Comment'                                      # column header

# Register Like admin
@admin.register(Like)
class LikeAdmin(admin.ModelAdmin):
    list_display = ['id', 'post', 'user', 'created']             # summary columns
    list_filter = ['created', 'post']                            # filter by creation and post
    search_fields = ['user__username', 'post__title']            # search by related user or post
    raw_id_fields = ['post', 'user']                             # raw id widgets for speed
    date_hierarchy = 'created'                                   # drill-down by like time
    ordering = ['-created']                                      # newest likes first
    show_facets = admin.ShowFacets.ALWAYS                        # show facets


@admin.register(Bookmark)
class BookmarkAdmin(admin.ModelAdmin):
    list_display = ['id', 'post', 'user', 'created']
    list_filter = ['created', 'post']
    search_fields = ['user__username', 'post__title']
    raw_id_fields = ['post', 'user']
    date_hierarchy = 'created'
    ordering = ['-created']
    show_facets = admin.ShowFacets.ALWAYS


@admin.register(NewsSource)
class NewsSourceAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'provider',
        'is_active',
        'auto_publish',
        'trust_score',
        'fetch_interval_minutes',
        'tracked_clicks',
        'updated',
    ]
    list_filter = ['provider', 'is_active', 'auto_publish']
    search_fields = ['name', 'notes', 'base_url']
    ordering = ['name']
    show_facets = admin.ShowFacets.ALWAYS
    actions = ['reset_click_metrics']

    def tracked_clicks(self, obj):
        return cache.get(f'analytics:clicks:source:{obj.pk}:total', 0)

    tracked_clicks.short_description = 'Clicks'

    @admin.action(description='Reset click metrics for selected sources')
    def reset_click_metrics(self, request, queryset):
        for source in queryset:
            _clear_source_click_metrics(source.pk)

        self.message_user(request, f"{queryset.count()} source click metric(s) reset.")


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = [
        'id',
        'title',
        'source',
        'status',
        'originality_score',
        'is_ad_safe',
        'fetched_at',
    ]
    list_filter = ['status', 'is_ad_safe', 'source__provider', 'source']
    search_fields = ['title', 'summary', 'body', 'source_url']
    raw_id_fields = ['source']
    date_hierarchy = 'fetched_at'
    ordering = ['-fetched_at']
    actions = [
        'queue_for_review',
        'publish_to_blog',
        'mark_rejected',
        'return_published_to_review',
    ]
    show_facets = admin.ShowFacets.ALWAYS

    @admin.action(description='Move selected articles to review queue')
    def queue_for_review(self, request, queryset):
        updated = queryset.exclude(status=Article.Status.PUBLISHED).update(
            status=Article.Status.PENDING_REVIEW
        )
        self.message_user(request, f"{updated} article(s) moved to review queue.")

    @admin.action(description='Publish selected articles to blog posts')
    def publish_to_blog(self, request, queryset):
        User = get_user_model()
        author = User.objects.filter(is_staff=True).order_by('id').first()
        if not author:
            self.message_user(
                request,
                "No staff user found. Create a staff user first.",
                level=messages.ERROR,
            )
            return

        created_count = 0
        for article in queryset.select_related('source'):
            if article.status == Article.Status.PUBLISHED:
                continue
            if Post.objects.filter(source_article=article).exists():
                continue

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
            )
            article.status = Article.Status.PUBLISHED
            article.save(update_fields=['status', 'updated'])
            created_count += 1

        self.message_user(request, f"{created_count} post(s) created from selected articles.")

    @admin.action(description='Mark selected articles as rejected')
    def mark_rejected(self, request, queryset):
        updated = queryset.exclude(status=Article.Status.PUBLISHED).update(
            status=Article.Status.REJECTED
        )
        self.message_user(request, f"{updated} article(s) marked as rejected.")

    @admin.action(description='Return published articles to review')
    def return_published_to_review(self, request, queryset):
        updated = queryset.filter(status=Article.Status.PUBLISHED).update(
            status=Article.Status.PENDING_REVIEW
        )
        self.message_user(request, f"{updated} published article(s) moved back to review.")


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ['email', 'is_active', 'last_sent_at', 'created', 'updated']
    list_filter = ['is_active', 'created', 'updated']
    search_fields = ['email']
    ordering = ['-created']
    show_facets = admin.ShowFacets.ALWAYS
