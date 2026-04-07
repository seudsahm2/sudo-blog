from django.core.management.base import BaseCommand

from blog.services.launch_readiness import compute_launch_readiness_checks


class Command(BaseCommand):
    help = "Show launch-readiness checks for production configuration and feature flags."

    def handle(self, *args, **options):
        report = compute_launch_readiness_checks()

        self.stdout.write("Launch Readiness Report")
        self.stdout.write("=" * 24)
        for item in report["checks"]:
            name = item["name"]
            ok = item["ok"]
            detail = item["detail"]
            status = "PASS" if ok else "FAIL"
            self.stdout.write(f"[{status}] {name}: {detail}")

        self.stdout.write("-" * 24)
        self.stdout.write(
            f"Summary: pass={report['pass_count']}, fail={report['fail_count']}, total={report['total_count']}"
        )

        if report["fail_count"] > 0:
            self.stdout.write(self.style.WARNING("Launch readiness has failing checks."))
        else:
            self.stdout.write(self.style.SUCCESS("Launch readiness checks passed."))
