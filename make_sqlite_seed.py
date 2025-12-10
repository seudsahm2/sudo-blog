# make_sqlite_seed.py
# Generates a SQLite-compatible SQL seed file for a blog using django-taggit.
# Produces: auth_user, blog_category, taggit_tag, blog_post, taggit_taggeditem, blog_comment, blog_like
#
# WARNING: The generated SQL will DELETE rows from the target tables before inserting.
#          BACKUP your db.sqlite3 if you care about existing data.
#
# Usage:
#   1) Run migrations first: python manage.py migrate
#   2) Generate file: python make_sqlite_seed.py
#   3) Import: sqlite3 db.sqlite3 < blog_seed_sqlite.sql
#
# The script inserts explicit IDs and therefore resets sequences. That's intentional for reproducible seeds.

import random
import hashlib
from datetime import datetime, timedelta, timezone
import os

# ---- CONFIG ----
OUT_FILENAME = "blog_seed_sqlite.sql"   # output SQL file
NUM_USERS = 1000
NUM_CATEGORIES = 20  # Reduced for meaningful categorization
NUM_TAGS = 50        # Reduced significantly to ensure overlaps for "Similar Posts"
NUM_POSTS = 1000
NUM_COMMENTS = 1000
NUM_LIKES = 1000
AVG_TAGS_PER_POST = 3   # Increased slightly


random.seed(42)  # deterministic output for repeatability
now = datetime.now(timezone.utc)  # timezone-aware UTC now

# Subquery to find content_type id for blog.Post at import time (requires migrations run)
CONTENT_TYPE_SUBQUERY = "(SELECT id FROM django_content_type WHERE app_label='blog' AND model='post')"

def safe_sql_string(s):
    """Escape single quotes for SQL literals."""
    return s.replace("'", "''")

def iso(dt):
    """Format datetimes as SQLite-friendly strings (YYYY-MM-DD HH:MM:SS)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# Build SQL
lines = []
lines.append("-- SQLite seed SQL generated for django-taggit (blog)")
lines.append("-- WARNING: this file deletes rows from target tables before inserting. Backup first if needed.")
lines.append("PRAGMA foreign_keys = OFF;")  # disable foreign keys during cleanup/inserts for sqlite

# --- safe deletes (clear tables so explicit ids do not clash) ---
# Order matters: clear dependent tables first
lines.append("-- Clear existing rows (use with caution)")
for table in [
    "taggit_taggeditem",
    "taggit_tag",
    "blog_like",
    "blog_comment",
    "blog_post",
    "blog_category",
    "auth_user",
]:
    lines.append(f"DELETE FROM {table};")
    # reset sqlite_sequence for autoincrement primary keys
    lines.append(f"DELETE FROM sqlite_sequence WHERE name='{table}';")

lines.append("PRAGMA foreign_keys = ON;")
lines.append("")  # blank line

# --- auth_user rows ---
lines.append("-- auth_user rows")
for i in range(1, NUM_USERS + 1):
    username = f"user{i}"
    email = f"user{i}@example.com"
    password = "pbkdf2_sha256$260000$" + hashlib.md5(username.encode()).hexdigest()
    is_super = 1 if i == 1 else 0
    is_staff = 1 if i <= 10 else 0
    is_active = 1
    date_joined = iso(now - timedelta(days=random.randint(0, 1000)))
    first_name = f"First{i}"
    last_name = f"Last{i}"
    lines.append(
        "INSERT INTO auth_user (id, password, last_login, is_superuser, username, first_name, last_name, email, is_staff, is_active, date_joined) VALUES "
        f"({i}, '{safe_sql_string(password)}', NULL, {is_super}, '{safe_sql_string(username)}', '{safe_sql_string(first_name)}', '{safe_sql_string(last_name)}', '{safe_sql_string(email)}', {is_staff}, {is_active}, '{date_joined}');"
    )
lines.append("")

# --- blog_category rows ---
lines.append("-- blog_category rows")
for i in range(1, NUM_CATEGORIES + 1):
    name = f"Category {i}"
    slug = f"category-{i}"
    lines.append(f"INSERT INTO blog_category (id, name, slug) VALUES ({i}, '{safe_sql_string(name)}', '{safe_sql_string(slug)}');")
lines.append("")

# --- taggit_tag rows ---
# taggit_tag default fields: id, name, slug
lines.append("-- taggit_tag rows")
for i in range(1, NUM_TAGS + 1):
    name = f"tag{i}"
    slug = f"tag-{i}"
    lines.append(f"INSERT INTO taggit_tag (id, name, slug) VALUES ({i}, '{safe_sql_string(name)}', '{safe_sql_string(slug)}');")
lines.append("")

# --- blog_post rows ---
# Note: ensure your blog_post table columns match this ordering (id, title, slug, author_id, body, publish, created, updated, status, category_id)
lines.append("-- blog_post rows")
for i in range(1, NUM_POSTS + 1):
    title = f"Seeded Post {i}"
    slug = f"seeded-post-{i}"
    author_id = random.randint(1, NUM_USERS)
    # ~90% categorized, 10% uncategorized
    if random.random() < 0.9:
        category_id = random.randint(1, NUM_CATEGORIES)
        category_val = str(category_id)
    else:
        category_val = "NULL"
    body = ("This is the seeded body for post " + str(i) + ". ") * (1 + (i % 5))
    publish_dt = now - timedelta(days=random.randint(-30, 720), hours=random.randint(0, 23), minutes=random.randint(0, 59))
    created_dt = publish_dt - timedelta(days=random.randint(0, 30))
    updated_dt = created_dt + timedelta(days=random.randint(0, 30))
    status = "PB" if random.random() < 0.65 else "DF"
    lines.append(
        "INSERT INTO blog_post (id, title, slug, author_id, body, publish, created, updated, status, category_id) VALUES "
        f"({i}, '{safe_sql_string(title)}', '{safe_sql_string(slug)}', {author_id}, '{safe_sql_string(body)}', '{iso(publish_dt)}', '{iso(created_dt)}', '{iso(updated_dt)}', '{status}', {category_val});"
    )
lines.append("")

# --- taggit_taggeditem rows (connect tags to blog.Post) ---
lines.append("-- taggit_taggeditem rows (link tags to blog.Post using django_content_type)")
taggeditem_id = 1
for post_id in range(1, NUM_POSTS + 1):
    # choose 1 or 2 tags usually, sometimes 3
    num_tags = AVG_TAGS_PER_POST + (1 if random.random() < 0.25 else 0)
    chosen = set()
    attempts = 0
    while len(chosen) < num_tags and attempts < 50:
        attempts += 1
        t = random.randint(1, NUM_TAGS)
        if t in chosen:
            continue
        chosen.add(t)
        # INSERT id, tag_id, content_type_id (subquery), object_id (post pk)
        lines.append(
            "INSERT INTO taggit_taggeditem (id, tag_id, content_type_id, object_id) VALUES "
            f"({taggeditem_id}, {t}, {CONTENT_TYPE_SUBQUERY}, {post_id});"
        )
        taggeditem_id += 1
lines.append("")

# --- blog_comment rows ---
lines.append("-- blog_comment rows")
for i in range(1, NUM_COMMENTS + 1):
    post_id = ((i - 1) % NUM_POSTS) + 1
    user_id = random.randint(1, NUM_USERS)
    body = f"Seed comment {i} on post {post_id}."
    created_dt = now - timedelta(days=random.randint(0, 900), hours=random.randint(0, 23), minutes=random.randint(0, 59))
    updated_dt = created_dt + timedelta(hours=random.randint(0, 200))
    approved = 1 if random.random() < 0.5 else 0
    lines.append(
        "INSERT INTO blog_comment (id, post_id, user_id, body, created, updated, approved) VALUES "
        f"({i}, {post_id}, {user_id}, '{safe_sql_string(body)}', '{iso(created_dt)}', '{iso(updated_dt)}', {approved});"
    )
lines.append("")

# --- blog_like rows ---
lines.append("-- blog_like rows")
likes_set = set()
like_id = 1
attempts = 0
max_attempts = NUM_LIKES * 10
while like_id <= NUM_LIKES and attempts < max_attempts:
    attempts += 1
    post_id = random.randint(1, NUM_POSTS)
    user_id = random.randint(1, NUM_USERS)
    pair = (post_id, user_id)
    if pair in likes_set:
        continue
    likes_set.add(pair)
    created_dt = now - timedelta(days=random.randint(0, 900), hours=random.randint(0, 23), minutes=random.randint(0, 59))
    lines.append(
        "INSERT INTO blog_like (id, post_id, user_id, created) VALUES "
        f"({like_id}, {post_id}, {user_id}, '{iso(created_dt)}');"
    )
    like_id += 1

lines.append("")
lines.append("-- End of seed SQL")
# join and write to file
with open(OUT_FILENAME, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"Done. Wrote {OUT_FILENAME} with seed data (users:{NUM_USERS}, categories:{NUM_CATEGORIES}, taggit_tags:{NUM_TAGS}, posts:{NUM_POSTS}, comments:{NUM_COMMENTS}, likes:{len(likes_set)}, taggeditems:{taggeditem_id-1})")
