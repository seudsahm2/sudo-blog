import os
import django
from django.db.models import Count

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sudo_blog.settings")
django.setup()

from blog.models import Post
from taggit.models import Tag

def diagnose():
    print("--- DIAGNOSTICS START ---")
    
    total_posts = Post.objects.count()
    published_posts = Post.published.count()
    total_tags = Tag.objects.count()
    
    print(f"Total Posts: {total_posts}")
    print(f"Published Posts: {published_posts}")
    print(f"Total Tags: {total_tags}")
    
    if published_posts == 0:
        print("No published posts found!")
        return

    # Check Tag Distribution
    tagged_posts = Post.objects.filter(tags__isnull=False).distinct().count()
    print(f"Posts with at least 1 tag: {tagged_posts}")

    # Pick a sample post that HAS tags
    sample_post = Post.published.filter(tags__isnull=False).first()
    
    if not sample_post:
        print("No published posts have tags.")
        return

    print(f"\nAnalyzing Sample Post: '{sample_post.title}' (ID: {sample_post.id})")
    print(f"Slug: {sample_post.slug}")
    
    tags = sample_post.tags.all()
    tag_names = list(tags.values_list('name', flat=True))
    tag_ids = list(tags.values_list('id', flat=True))
    print(f"Tags: {tag_names} (IDs: {tag_ids})")
    
    # Run the exact query from views.py
    post_tags_ids = sample_post.tags.values_list('id', flat=True)
    similar_posts_query = Post.published.filter(tags__in=post_tags_ids).exclude(id=sample_post.id)
    
    raw_count = similar_posts_query.count()
    print(f"Similar Posts Found (Raw Query): {raw_count}")
    
    # Annotated query
    similar_posts_annotated = similar_posts_query.annotate(same_tags=Count('tags')).order_by('-same_tags', '-publish')[:4]
    annotated_list = list(similar_posts_annotated)
    
    print(f"Similar Posts (Annotated & Sliced): {len(annotated_list)}")
    for p in annotated_list:
        common = p.tags.filter(id__in=tag_ids).count()
        print(f" - {p.title} (ID: {p.id}) - Shared Tags: {common}")

    # Check overall overlap probability
    print("\n--- Overlap Check ---")
    if total_tags > 0:
        avg_tags_per_post = Post.tags.through.objects.count() / total_posts
        print(f"Avg Tags per Post: {avg_tags_per_post:.2f}")
        print(f"Tag Space: {total_tags}")
        if total_tags > 100 and avg_tags_per_post < 3:
            print("WARNING: High tag count with low density implies VERY LOW overlap probability.")

if __name__ == "__main__":
    diagnose()
