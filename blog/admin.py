# blog/admin.py
from django.contrib import admin
from .models import Post, Category, Tag, Comment, Like

@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ['title', 'slug', 'author', 'publish', 'status']   # columns shown in changelist
    list_filter = ['status', 'created', 'publish', 'author', 'tags']  # add tags filter
    search_fields = ['title', 'body']
    prepopulated_fields = {'slug': ('title',)}
    raw_id_fields = ['author']
    date_hierarchy = 'publish'
    ordering = ['status', 'publish']
    filter_horizontal = ['tags']                                      # for easy many-to-many editing
    show_facets = admin.ShowFacets.ALWAYS

# Register Category admin
@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']                                   # show name and slug
    search_fields = ['name']                                           # allow searching by name
    prepopulated_fields = {'slug': ('name',)}                          # auto-fill slug from name
    ordering = ['name']                                                # sort categories alphabetically
    show_facets = admin.ShowFacets.ALWAYS                              # show facets

# Register Tag admin
@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']                                   # show name and slug
    search_fields = ['name']                                           # search by tag name
    prepopulated_fields = {'slug': ('name',)}                          # auto-fill slug from name
    ordering = ['name']                                                # alphabetical order
    show_facets = admin.ShowFacets.ALWAYS                              # show facets

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
