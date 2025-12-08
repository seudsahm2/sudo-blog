from django.core.management.base import BaseCommand         # import base command class
from django.db import connection                            # gives us direct access to SQL cursor
from django.conf import settings                            # lets us find project directory
import os                                                   # used for building file paths

class Command(BaseCommand):                                 # define new django management command
    help = "Seeds the database using the generated SQLite SQL file"   # command description

    def handle(self, *args, **options):                     # entry-point for this command
        # build the absolute path to the SQL file
        sql_path = os.path.join(settings.BASE_DIR, "seed", "blog_seed_sqlite.sql")

        # check if the file exists before running
        if not os.path.exists(sql_path):
            self.stdout.write(self.style.ERROR(f"SQL file not found at: {sql_path}"))
            return

        self.stdout.write(self.style.WARNING("Reading SQL file..."))

        # read full SQL content
        with open(sql_path, "r", encoding="utf-8") as f:
            sql_content = f.read()

        self.stdout.write(self.style.WARNING("Executing SQL statements..."))

        # use database cursor to execute raw SQL
        with connection.cursor() as cursor:
            cursor.executescript(sql_content)              # executes multiple SQL statements at once

        self.stdout.write(self.style.SUCCESS("Database seeding completed successfully!"))
