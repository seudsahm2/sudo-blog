from django.shortcuts import render, get_object_or_404, redirect
from .models import Post, Like
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.views.generic import ListView
from .forms import EmailPostForm, CommentForm
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

class PostListView(ListView):
    queryset = Post.published.all()
    context_object_name = "posts"
    paginate_by = 6
    template_name = "blog/post/list.html"

def post_detail(request, year, month, day, post):
    post = get_object_or_404(
        Post,
        publish__year=year,
        publish__month=month,
        publish__day=day,
        slug=post,
        status=Post.Status.PUBLISHED
    )
    
    is_liked = False
    if request.user.is_authenticated:
        is_liked = Like.objects.filter(post=post, user=request.user).exists()
        
    return render(request, "blog/post/detail.html", {
        "post": post,
        "form": CommentForm(),
        "is_liked": is_liked
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
            "sent": sent
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

from django.http import JsonResponse

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
