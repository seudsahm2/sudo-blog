from django.shortcuts import render,get_object_or_404
from .models import Post
# Create your views here.

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.views.generic import ListView
from .forms import EmailPostForm

class PostListView(ListView):
    queryset = Post.published.all()
    context_object_name = "posts"
    paginate_by = 6
    template_name = "blog/post/list.html"
    
# def post_list(request):
    # Search Logic
    # query = request.GET.get('q')
    # if query:
    #     object_list = Post.published.filter(
    #         Q(title__icontains=query) | Q(body__icontains=query)
    #     )
    # else:
    #     object_list = Post.published.all()

    # Pagination Logic
    # paginator = Paginator(object_list, 6) # 6 posts per page
    # page = request.GET.get('page')
    # try:
    #     posts = paginator.page(page)
    # except PageNotAnInteger:
    #     posts = paginator.page(1)
    # except EmptyPage:
    #     posts = paginator.page(paginator.num_pages)
        
    # return render(
    #     request,
    #     "blog/post/list.html",
    #     {"posts": posts, "query": query}
    # )

def post_detail(request,year,month,day,post):
    # try:
    #     post = Post.published.get(id=id)
    # except Post.DoesNotExist:
    #     return Http404("No Post Found")
    # return render(request,"blog/post/detail.html",{"post":post})

    post = get_object_or_404(
        Post,
        publish__year = year,
        publish__month = month,
        publish__day = day,
        slug = post,
        status=Post.Status.PUBLISHED
    )
    return render(request,"blog/post/detail.html",{"post":post})


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