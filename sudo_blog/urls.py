"""
URL configuration for sudo_blog project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path,include
from django.http import HttpResponse
from blog import views as blog_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("blog/",include("blog.urls",namespace="blog")),
    path("healthz", lambda request: HttpResponse("ok", content_type="text/plain"), name="healthz"),
    path("privacy-policy/", blog_views.legal_page, {'page': 'privacy'}, name='privacy_policy'),
    path("about/", blog_views.legal_page, {'page': 'about'}, name='about_page'),
    path("disclaimer/", blog_views.legal_page, {'page': 'disclaimer'}, name='disclaimer_page'),
    path("contact/", blog_views.legal_page, {'page': 'contact'}, name='contact_page'),
    path("ads.txt", blog_views.ads_txt, name='ads_txt_root'),
    path("robots.txt", blog_views.robots_txt, name='robots_txt_root'),
    path("sitemap.xml", blog_views.sitemap_xml, name='sitemap_xml_root'),
]
