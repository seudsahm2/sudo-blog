# blog/management/commands/blog_seed_sqlite.py
# Management command: python manage.py blog_seed_sqlite
# Executes a SQLite-compatible SQL file to seed the DB.
# IMPORTANT: put your SQL file at <project_root>/seed/blog_seed_sqlite.sql

from django.core.management.base import BaseCommand        # base class for custom management commands
from django.db import connection                           # low-level DB connection (Django DB-API wrapper)
from django.conf import settings                           # to build absolute path using BASE_DIR
import os                                                  # path utilities
import sys                                                 # used for exception printing

class Command(BaseCommand):                                # Django expects a top-level Command class
    help = "Seed the database using seed/blog_seed_sqlite.sql (SQLite-compatible SQL file)"

    def add_arguments(self, parser):
        # optional argument to point to a different SQL file
        parser.add_argument(
            "--file",
            "-f",
            help="Path to SQL file (defaults to <BASE_DIR>/seed/blog_seed_sqlite.sql)",
            default=os.path.join(settings.BASE_DIR, "seed", "blog_seed_sqlite.sql"),
        )

    def handle(self, *args, **options):
        # resolve SQL file path
        sql_path = options.get("file")
        self.stdout.write(self.style.NOTICE(f"Using SQL file: {sql_path}"))

        # check file existence
        if not os.path.exists(sql_path):
            self.stderr.write(self.style.ERROR(f"SQL file not found: {sql_path}"))
            self.stderr.write(self.style.ERROR("Make sure the file exists and you used the correct path."))
            return

        # read content
        try:
            with open(sql_path, "r", encoding="utf-8") as fh:
                sql_content = fh.read()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to read SQL file: {e}"))
            return

        # If file is huge you might want to execute in chunks. For SQLite we can use executescript.
        self.stdout.write(self.style.WARNING("Starting SQL execution..."))
        try:
            # connection.cursor() returns a DB-API cursor object. For sqlite3 that cursor supports executescript().
            with connection.cursor() as cursor:
                # If the underlying DB is SQLite we can safely use executescript to run many statements.
                # For other DB engines, executescript may not exist; fallback to executing statements separately.
                if hasattr(cursor, "executescript"):
                    cursor.executescript(sql_content)
                else:
                    # fallback: split on semicolon and execute statements one by one
                    statements = [s.strip() for s in sql_content.split(";") if s.strip()]
                    for stmt in statements:
                        cursor.execute(stmt)
        except Exception as exc:
            # print a helpful error and the SQL nearby (not all of it)
            self.stderr.write(self.style.ERROR("SQL execution failed. See details below:"))
            self.stderr.write(self.style.ERROR(str(exc)))
            # optionally show the last 500 chars of the file to help debug syntax issues
            snippet = (sql_content[-500:]) if len(sql_content) > 500 else sql_content
            self.stderr.write(self.style.ERROR("SQL tail (last ~500 chars):"))
            self.stderr.write(self.style.ERROR(snippet))
            return

        self.stdout.write(self.style.SUCCESS("Seeding completed successfully."))
