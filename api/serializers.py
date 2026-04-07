from rest_framework import serializers

from blog.models import Article, Category, Comment, NewsSource, NewsletterSubscriber, Post


class CategoryListSerializer(serializers.ModelSerializer):
    posts_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "posts_count"]


class PostListSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    tags = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")

    class Meta:
        model = Post
        fields = [
            "id",
            "title",
            "slug",
            "summary",
            "cover_image_url",
            "publish",
            "category",
            "tags",
            "auto_generated",
        ]

    def get_category(self, obj):
        if not obj.category_id:
            return {"name": "Others", "slug": "others"}
        return {"name": obj.category.name, "slug": obj.category.slug}


class PostDetailSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    tags = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")
    author = serializers.CharField(source="author.username", read_only=True)
    read_time_minutes = serializers.SerializerMethodField()
    source_name = serializers.CharField(source="source_article.source.name", read_only=True)

    class Meta:
        model = Post
        fields = [
            "id",
            "title",
            "slug",
            "body",
            "summary",
            "cover_image_url",
            "publish",
            "updated",
            "author",
            "category",
            "tags",
            "auto_generated",
            "source_name",
            "read_time_minutes",
        ]

    def get_category(self, obj):
        if not obj.category_id:
            return {"name": "Others", "slug": "others"}
        return {"name": obj.category.name, "slug": obj.category.slug}

    def get_read_time_minutes(self, obj):
        return obj.get_read_time()


class CommentSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = Comment
        fields = ["id", "body", "created", "updated", "approved", "user"]
        read_only_fields = ["id", "created", "updated", "approved", "user"]


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True)


class CurrentUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField()
    is_staff = serializers.BooleanField()


class NewsSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsSource
        fields = [
            "id",
            "name",
            "provider",
            "is_active",
            "auto_publish",
            "trust_score",
            "fetch_interval_minutes",
            "base_url",
            "notes",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "created", "updated"]


class ArticleSourceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    provider = serializers.CharField()


class ArticleListSerializer(serializers.ModelSerializer):
    source = serializers.SerializerMethodField()

    class Meta:
        model = Article
        fields = [
            "id",
            "title",
            "slug",
            "status",
            "source",
            "source_url",
            "published_at",
            "fetched_at",
            "originality_score",
            "is_ad_safe",
            "language",
            "summary",
            "summary_provider",
            "summary_model",
            "summary_category",
            "summary_prompt_mode",
            "summary_prompt_tokens",
            "summary_completion_tokens",
            "summary_total_tokens",
            "summary_estimated_cost_usd",
        ]

    def get_source(self, obj):
        return {
            "id": obj.source_id,
            "name": obj.source.name,
            "provider": obj.source.provider,
        }


class ArticleDetailSerializer(ArticleListSerializer):
    class Meta(ArticleListSerializer.Meta):
        fields = ArticleListSerializer.Meta.fields + [
            "body",
            "image_url",
            "external_id",
            "content_hash",
            "created",
            "updated",
        ]


class PipelineFetchSerializer(serializers.Serializer):
    source_id = serializers.IntegerField(required=False, min_value=1)
    max_items = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)


class PipelineLimitSerializer(serializers.Serializer):
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)


class PipelineStepSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["fetch", "summarize", "publish", "rollback"])
    source_id = serializers.IntegerField(required=False, min_value=1)
    max_items = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)


class PipelineRunSerializer(serializers.Serializer):
    steps = PipelineStepSerializer(many=True, min_length=1, max_length=10)


class NewsletterEmailSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class NewsletterDigestTriggerSerializer(serializers.Serializer):
    hours = serializers.IntegerField(required=False, min_value=1, max_value=720, default=48)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=25, default=8)


class NewsletterSubscriberSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsletterSubscriber
        fields = ["id", "email", "is_active", "last_sent_at", "created", "updated"]
        read_only_fields = ["id", "email", "last_sent_at", "created", "updated"]


class AnalyticsTopPostSerializer(serializers.Serializer):
    post_id = serializers.IntegerField(source="post.id")
    title = serializers.CharField(source="post.title")
    clicks = serializers.IntegerField()
    source_clicks = serializers.IntegerField()
    publish = serializers.DateTimeField(source="post.publish")


class AnalyticsTopSourceSerializer(serializers.Serializer):
    source_id = serializers.IntegerField(source="source.id")
    name = serializers.CharField(source="source.name")
    provider = serializers.CharField(source="source.provider")
    clicks = serializers.IntegerField()


class MonitoringTaskSnapshotSerializer(serializers.Serializer):
    task = serializers.CharField()
    last_status = serializers.CharField()
    last_run_at = serializers.DateTimeField(allow_null=True)
    last_success_at = serializers.DateTimeField(allow_null=True)
    last_failure_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(allow_blank=True)
    total_runs = serializers.IntegerField()
    total_failures = serializers.IntegerField()
    total_retries = serializers.IntegerField()
    consecutive_failures = serializers.IntegerField()


class SportsFixtureSerializer(serializers.Serializer):
    home = serializers.CharField()
    away = serializers.CharField()
    kickoff = serializers.CharField(allow_blank=True)
    score = serializers.CharField(allow_blank=True)


class SportsTableRowSerializer(serializers.Serializer):
    rank = serializers.CharField(allow_blank=True)
    team = serializers.CharField(allow_blank=True)
    points = serializers.IntegerField()
    goals = serializers.IntegerField()
    conceded = serializers.IntegerField()
    matches = serializers.IntegerField()
