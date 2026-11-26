from collections import Counter
from datetime import timedelta
from statistics import median

from django.contrib.auth import get_user_model
from django.db.models import F, Min
from django.db.models.expressions import RawSQL
from django.utils import timezone

from .models import IAvesFeedback


def report(days):
    end = timezone.now()
    start = end - timedelta(days=days)
    admitted = RawSQL("SELECT account_id FROM hub_admin.iaves_customer_signups(%s)", [start])
    accounts = get_user_model().objects.filter(pk__in=admitted, is_active=True,
                                                date_joined__gte=start, date_joined__lt=end)
    signups = accounts.count()
    feedback = IAvesFeedback.objects.filter(user__in=accounts).filter(
        consulted_at__gte=F("user__date_joined"), consulted_at__lt=end)
    first_use = feedback.filter(helpful=True).values("user_id", "user__date_joined").annotate(first=Min("consulted_at"))
    minutes = [(row["first"] - row["user__date_joined"]).total_seconds() / 60 for row in first_use]
    needs_review = Counter()
    for paths in feedback.filter(helpful=False).values_list("source_paths", flat=True):
        needs_review.update(set(paths))
    median_minutes = round(median(minutes)) if minutes else None
    return {"days":days,"signups":signups,"got_help":len(minutes),
            "percentage":round(len(minutes) * 100 / signups) if signups else None,
            "median_minutes":median_minutes,"notes":needs_review.most_common(5)}
