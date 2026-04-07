from django.conf import settings
from django.contrib.auth import get_user_model

from blog.models import NewsSource


def compute_launch_readiness_checks():
    checks = []

    def add_check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add_check(
        "debug_disabled",
        not bool(getattr(settings, "DEBUG", True)),
        f"DEBUG={getattr(settings, 'DEBUG', True)}",
    )

    allowed_hosts = getattr(settings, "ALLOWED_HOSTS", []) or []
    add_check(
        "allowed_hosts_configured",
        len(allowed_hosts) > 0,
        f"ALLOWED_HOSTS={allowed_hosts}",
    )

    use_postgres = bool(getattr(settings, "USE_POSTGRES", False))
    add_check(
        "postgres_enabled",
        use_postgres,
        f"USE_POSTGRES={use_postgres}",
    )

    secret_key = getattr(settings, "SECRET_KEY", "").strip()
    add_check(
        "secret_key_set",
        bool(secret_key),
        "SECRET_KEY present" if secret_key else "SECRET_KEY missing",
    )

    flags = {
        "FEATURE_FLAG_INGESTION_ENABLED": bool(getattr(settings, "FEATURE_FLAG_INGESTION_ENABLED", True)),
        "FEATURE_FLAG_SUMMARIZATION_ENABLED": bool(getattr(settings, "FEATURE_FLAG_SUMMARIZATION_ENABLED", True)),
        "FEATURE_FLAG_AUTOPUBLISH_ENABLED": bool(getattr(settings, "FEATURE_FLAG_AUTOPUBLISH_ENABLED", True)),
        "FEATURE_FLAG_ROLLBACK_ENABLED": bool(getattr(settings, "FEATURE_FLAG_ROLLBACK_ENABLED", True)),
        "FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED": bool(getattr(settings, "FEATURE_FLAG_NEWSLETTER_DIGEST_ENABLED", True)),
        "FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED": bool(getattr(settings, "FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED", False)),
        "FEATURE_FLAG_TELEGRAM_AUTOPUBLISH_ENABLED": bool(getattr(settings, "FEATURE_FLAG_TELEGRAM_AUTOPUBLISH_ENABLED", False)),
    }

    for flag_name, enabled in flags.items():
        add_check(flag_name.lower(), True, f"{flag_name}={enabled}")

    provider_keys = {
        NewsSource.Provider.NEWSAPI: bool(getattr(settings, "NEWSAPI_KEY", "").strip()),
        NewsSource.Provider.GNEWS: bool(getattr(settings, "GNEWS_KEY", "").strip()),
        NewsSource.Provider.MEDIASTACK: bool(getattr(settings, "MEDIASTACK_KEY", "").strip()),
        NewsSource.Provider.NEWSDATA: bool(getattr(settings, "NEWSDATA_KEY", "").strip()),
        NewsSource.Provider.GUARDIAN: bool(getattr(settings, "GUARDIAN_KEY", "").strip()),
    }

    active_sources = list(NewsSource.objects.filter(is_active=True).values("name", "provider"))
    for source in active_sources:
        provider = source["provider"]
        if provider in provider_keys:
            add_check(
                f"provider_key_{source['name']}",
                provider_keys[provider],
                f"source={source['name']} provider={provider} key_present={provider_keys[provider]}",
            )
        elif provider == NewsSource.Provider.TELEGRAM:
            enabled = bool(getattr(settings, "FEATURE_FLAG_TELEGRAM_INGESTION_ENABLED", False))
            add_check(
                f"telegram_source_{source['name']}",
                True,
                f"source={source['name']} provider=TELEGRAM ingestion_enabled={enabled}",
            )

    User = get_user_model()
    has_staff = User.objects.filter(is_staff=True, is_active=True).exists()
    add_check("active_staff_user_exists", has_staff, f"active_staff_user_exists={has_staff}")

    pass_count = sum(1 for item in checks if item["ok"])
    fail_count = len(checks) - pass_count

    return {
        "checks": checks,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "total_count": len(checks),
    }
