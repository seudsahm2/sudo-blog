from django.conf import settings                 
from django.db import models                    
from django.utils import timezone                
from django.urls import reverse
from decimal import Decimal

from taggit.managers import TaggableManager

class PublishedManager(models.Manager):
    def get_queryset(self):
        return (
            super().get_queryset().filter(status=Post.Status.PUBLISHED)
        )

class Post(models.Model):
    class Status(models.TextChoices):            
        DRAFT = 'DF', 'Draft'
        PUBLISHED = 'PB', 'Published'

    title = models.CharField(max_length=255)     
    slug = models.SlugField(
        max_length=255,
        unique_for_date = "publish"
    )      
    author = models.ForeignKey(                   
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blog_posts"
    )
    body = models.TextField()                   
    publish = models.DateTimeField(default=timezone.now)  
    created = models.DateTimeField(auto_now_add=True)    
    updated = models.DateTimeField(auto_now=True)        
    status = models.CharField(                   
        max_length=2,
        choices=Status.choices,
        default=Status.DRAFT
    )
    summary = models.TextField(blank=True)
    cover_image_url = models.URLField(blank=True)
    auto_generated = models.BooleanField(default=False)
    source_article = models.ForeignKey(
        'Article',
        on_delete=models.SET_NULL,
        related_name='generated_posts',
        null=True,
        blank=True,
    )


    category = models.ForeignKey(                
        'Category',
        on_delete=models.PROTECT,                
        related_name='posts',                    
        null=True,                                
        blank=True
    )

    objects = models.Manager()
    published = PublishedManager()
    tags = TaggableManager()

    class Meta:
        ordering = ['-publish']                   
        indexes = [models.Index(fields=['-publish'])] 

    def __str__(self):
        return self.title or f"Post {self.pk}"

    def get_absolute_url(self):
        return reverse(
            "blog:post_detail",
            args = [
                self.publish.year,
                self.publish.month,
                self.publish.day,
                self.slug
            ]
        )

    def get_read_time(self):
        from math import ceil
        return ceil(len(self.body.split()) / 200.0)     


class Category(models.Model):
    name = models.CharField(max_length=100)       
    slug = models.SlugField(max_length=100, unique=True)  

    def __str__(self):
        return self.name


class NewsSource(models.Model):
    class Provider(models.TextChoices):
        NEWSAPI = 'NEWSAPI', 'NewsAPI'
        GNEWS = 'GNEWS', 'GNews'
        MEDIASTACK = 'MEDIASTACK', 'MediaStack'
        TELEGRAM = 'TELEGRAM', 'Telegram'
        CUSTOM = 'CUSTOM', 'Custom'

    name = models.CharField(max_length=120, unique=True)
    provider = models.CharField(max_length=20, choices=Provider.choices)
    is_active = models.BooleanField(default=True)
    auto_publish = models.BooleanField(default=False)
    trust_score = models.PositiveSmallIntegerField(default=50)
    fetch_interval_minutes = models.PositiveIntegerField(default=60)
    base_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.provider})"


class Article(models.Model):
    class Status(models.TextChoices):
        INGESTED = 'ING', 'Ingested'
        SUMMARIZED = 'SUM', 'Summarized'
        PENDING_REVIEW = 'REV', 'Pending Review'
        PUBLISHED = 'PUB', 'Published'
        REJECTED = 'REJ', 'Rejected'

    source = models.ForeignKey(
        NewsSource,
        on_delete=models.CASCADE,
        related_name='articles',
    )
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    body = models.TextField()
    image_url = models.URLField(blank=True)
    summary = models.TextField(blank=True)
    summary_provider = models.CharField(max_length=20, blank=True)
    summary_model = models.CharField(max_length=80, blank=True)
    summary_prompt_mode = models.CharField(max_length=20, blank=True)
    summary_prompt_tokens = models.PositiveIntegerField(default=0)
    summary_completion_tokens = models.PositiveIntegerField(default=0)
    summary_total_tokens = models.PositiveIntegerField(default=0)
    summary_estimated_cost_usd = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        default=Decimal('0.000000'),
    )
    source_url = models.URLField(unique=True)
    external_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=3,
        choices=Status.choices,
        default=Status.INGESTED,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)
    content_hash = models.CharField(max_length=64, blank=True)
    originality_score = models.PositiveSmallIntegerField(default=0)
    is_ad_safe = models.BooleanField(default=True)
    language = models.CharField(max_length=10, default='en')
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = models.Manager()

    class Meta:
        ordering = ['-fetched_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['-fetched_at']),
            models.Index(fields=['source', 'status']),
        ]

    def __str__(self):
        return self.title



class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")  
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)  
    body = models.TextField()                     
    created = models.DateTimeField(auto_now_add=True)  
    updated = models.DateTimeField(auto_now=True)     
    approved = models.BooleanField(default=True)     

    objects = models.Manager()

    class Meta:
        ordering = ["-created"]                   

    def __str__(self):
        return f"comment by {self.user} on {self.post}"


class Like(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="likes")  
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)   
    created = models.DateTimeField(auto_now_add=True) 

    objects = models.Manager()

    class Meta:
        unique_together = ('post', 'user')          

    def __str__(self):
        return f"{self.user} likes {self.post}"


class Bookmark(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='bookmarks')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bookmarks')
    created = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()

    class Meta:
        unique_together = ('post', 'user')
        ordering = ['-created']

    def __str__(self):
        return f"{self.user} bookmarked {self.post}"


class NewsletterSubscriber(models.Model):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return self.email
