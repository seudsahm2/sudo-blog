from django.urls import path
from . import views

app_name="blog"

urlpatterns = [
    path("",views.PostListView.as_view(),name="post_list"),
    path("<int:year>/<int:month>/<int:day>/<slug:post>/",views.post_detail,name="post_detail"),
    path("<int:year>/<int:month>/<int:day>/<slug:post>/social-image.svg", views.post_social_image, name="post_social_image"),
    path("<int:post_id>/share/",views.post_share,name="post_share"),
    path("<int:post_id>/comment/",views.post_comment,name="post_comment"),
    path("<int:post_id>/like/",views.post_like,name="post_like"),
    path("<int:post_id>/bookmark/",views.post_bookmark,name="post_bookmark"),
    path("bookmarks/", views.bookmarks_list, name="bookmarks_list"),
    path("newsletter/subscribe/", views.newsletter_subscribe, name="newsletter_subscribe"),
    path("analytics/click/", views.track_post_click, name="track_post_click"),
    path("analytics/", views.analytics_dashboard, name="analytics_dashboard"),
    path("analytics/health.json", views.monitoring_health, name="monitoring_health"),
    path("analytics/launch-readiness.json", views.launch_readiness_health, name="launch_readiness_health"),
    path("analytics/export.csv", views.analytics_export_csv, name="analytics_export_csv"),
    path("analytics/reset/", views.analytics_reset_all, name="analytics_reset_all"),
    path("analytics/run-pipeline/", views.run_manual_pipeline, name="run_manual_pipeline"),
    path("analytics/trending-snapshot.csv", views.analytics_export_trending_snapshot, name="analytics_export_trending_snapshot"),
    path("tag/<slug:tag_slug>/",views.PostListView.as_view(),name="post_list_by_tag"),
    path("tag/<slug:tag_slug>/social-image.svg", views.tag_social_image, name="tag_social_image"),
    path("legal/<slug:page>/", views.legal_page, name="legal_page"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap_xml"),
]