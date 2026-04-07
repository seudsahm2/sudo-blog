from django.conf import settings


def site_settings(_request):
    return {
        "site_name": "Stunning Blog",
        "adsense_enabled": getattr(settings, "ADSENSE_ENABLED", False),
        "adsense_client_id": getattr(settings, "ADSENSE_CLIENT_ID", ""),
        "adsense_slot_header": getattr(settings, "ADSENSE_SLOT_HEADER", ""),
        "adsense_slot_in_feed": getattr(settings, "ADSENSE_SLOT_IN_FEED", ""),
        "adsense_slot_in_article": getattr(settings, "ADSENSE_SLOT_IN_ARTICLE", ""),
        "adsense_slot_footer": getattr(settings, "ADSENSE_SLOT_FOOTER", ""),
        "adsense_slot_trending": getattr(settings, "ADSENSE_SLOT_TRENDING", ""),
        "adsense_slot_below_content": getattr(settings, "ADSENSE_SLOT_BELOW_CONTENT", ""),
        "adsense_slot_sticky_mobile": getattr(settings, "ADSENSE_SLOT_STICKY_MOBILE", ""),
        "privacy_policy_url": "/privacy-policy/",
        "about_url": "/about/",
        "disclaimer_url": "/disclaimer/",
        "contact_url": "/contact/",
    }