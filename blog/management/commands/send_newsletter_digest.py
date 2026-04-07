from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.mail import send_mass_mail
from django.utils import timezone

from blog.models import NewsletterSubscriber
from blog.views import digest_posts_queryset


class Command(BaseCommand):
    help = 'Send a digest email with top recent stories to active newsletter subscribers.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=48)
        parser.add_argument('--limit', type=int, default=8)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if not getattr(settings, 'FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED', True):
            self.stdout.write(self.style.WARNING('Newsletter digest feature is disabled.'))
            return

        hours = options['hours']
        limit = options['limit']
        dry_run = options['dry_run']

        subscribers = list(
            NewsletterSubscriber.objects.filter(is_active=True)
            .values_list('email', flat=True)
            .order_by('email')
        )
        posts = digest_posts_queryset(hours=hours, limit=limit)

        if not subscribers:
            self.stdout.write(self.style.WARNING('No active subscribers.'))
            return

        if not posts:
            self.stdout.write(self.style.WARNING('No posts in digest window.'))
            return

        lines = ['Top stories from Stunning Blog:', '']
        for idx, post in enumerate(posts, start=1):
            lines.append(f"{idx}. {post.title}")
            lines.append(post.get_absolute_url())
            if post.summary:
                lines.append(post.summary[:180])
            lines.append('')

        body = '\n'.join(lines)
        subject = f"Stunning Blog Digest ({timezone.now().date().isoformat()})"
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@example.com')

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Dry run OK: would send digest to {len(subscribers)} subscriber(s) with {len(posts)} post(s)."
                )
            )
            return

        messages = [(subject, body, from_email, [email]) for email in subscribers]
        sent_count = send_mass_mail(messages, fail_silently=False)

        NewsletterSubscriber.objects.filter(email__in=subscribers).update(last_sent_at=timezone.now())
        self.stdout.write(self.style.SUCCESS(f"Sent digest emails: {sent_count}"))
