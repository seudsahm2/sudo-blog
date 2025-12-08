from django.conf import settings                 
from django.db import models                    
from django.utils import timezone                
from django.db.models.functions import Now       

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
    slug = models.SlugField(max_length=255)      
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


    category = models.ForeignKey(                
        'Category',
        on_delete=models.PROTECT,                
        related_name='posts',                    
        null=True,                                
        blank=True
    )

  
    tags = models.ManyToManyField(
        'Tag',
        related_name='posts',                     
        blank=True                                
    )

    objects = models.Manager()
    published = PublishedManager()

    class Meta:
        ordering = ['-publish']                   
        indexes = [models.Index(fields=['-publish'])] 

    def __str__(self):
        return self.title or f"Post {self.pk}"

    def get_read_time(self):
        from math import ceil
        return ceil(len(self.body.split()) / 200.0)     


class Category(models.Model):
    name = models.CharField(max_length=100)       
    slug = models.SlugField(max_length=100, unique=True)  

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(max_length=50)         
    slug = models.SlugField(max_length=100, unique=True)  

    def __str__(self):
        return self.name


class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")  
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)  
    body = models.TextField()                     
    created = models.DateTimeField(auto_now_add=True)  
    updated = models.DateTimeField(auto_now=True)     
    approved = models.BooleanField(default=False)     

    class Meta:
        ordering = ["-created"]                   

    def __str__(self):
        return f"comment by {self.user} on {self.post}"


class Like(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="likes")  
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)   
    created = models.DateTimeField(auto_now_add=True) 

    class Meta:
        unique_together = ('post', 'user')          

    def __str__(self):
        return f"{self.user} likes {self.post}"
