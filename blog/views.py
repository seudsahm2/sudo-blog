from django.shortcuts import render,get_object_or_404
from .models import Post
# Create your views here.

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q

def post_list(request):
    # Search Logic
    query = request.GET.get('q')
    if query:
        object_list = Post.published.filter(
            Q(title__icontains=query) | Q(body__icontains=query)
        )
    else:
        object_list = Post.published.all()

    # Pagination Logic
    paginator = Paginator(object_list, 6) # 6 posts per page
    page = request.GET.get('page')
    try:
        posts = paginator.page(page)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts = paginator.page(paginator.num_pages)
        
    return render(
        request,
        "blog/post/list.html",
        {"posts": posts, "query": query}
    )

def post_detail(request,id):
    # try:
    #     post = Post.published.get(id=id)
    # except Post.DoesNotExist:
    #     return Http404("No Post Found")
    # return render(request,"blog/post/detail.html",{"post":post})

    post = get_object_or_404(Post,id=id,status=Post.Status.PUBLISHED)
    return render(request,"blog/post/detail.html",{"post":post})
