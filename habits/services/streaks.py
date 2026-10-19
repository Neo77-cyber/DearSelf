import datetime
import logging

from django.utils import timezone

from ..models import DailyRecord, Habit, get_logical_date

logger = logging.getLogger(__name__)

# A logical day runs from 06:00 to 06:00 local time, so late-night clock-ins
# still count for the day before (see get_logical_date).
DAY_STARTS_AT_HOUR = 6


def day_start(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour=DAY_STARTS_AT_HOUR))
    )


def close_day(day: datetime.date = None) -> dict:
    """Record a miss for every active habit not clocked in on `day` (default:
    the logical day that just ended). Safe to run more than once: a missed
    DailyRecord marks the miss as counted, so a retried job can't double count.
    The day a habit is created is free, so late-night signups aren't punished."""
    if day is None:
        day = get_logical_date() - datetime.timedelta(days=1)

    habits = (
        Habit.objects.filter(user__is_active=True, created_at__lt=day_start(day))
        .exclude(records__date=day)
        .select_related("user__profile")
    )

    misses, newly_banned = 0, {}
    for habit in habits:
        _, created = DailyRecord.objects.get_or_create(
            habit=habit, date=day, defaults={"completed": False}
        )
        if not created:
            continue
        misses += 1
        if habit.record_miss()["status"] == "banned":
            newly_banned[habit.user_id] = habit.user
            logger.warning("User %s banned after 3 misses.", habit.user.username)

    return {"day": day, "misses": misses, "banned": list(newly_banned.values())}
