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
        Habit.objects.filter(
            user__is_active=True,
            created_at__lt=day_start(day),
            # A lost habit is finished: it collects no more misses.
            missed_count__lt=Habit.MISSES_TO_LOSE,
        )
        .exclude(records__date=day)
        .select_related("user__profile")
    )

    misses, lost, missed_users = 0, [], {}
    for habit in habits:
        _, created = DailyRecord.objects.get_or_create(
            habit=habit, date=day, defaults={"completed": False}
        )
        if not created:
            continue
        misses += 1
        missed_users[habit.user_id] = habit.user
        if habit.record_miss()["status"] == "lost":
            lost.append(habit)

    # You're out once you've lost 3 habits.
    banned = []
    for user in missed_users.values():
        lost_count = Habit.objects.filter(
            user=user, missed_count__gte=Habit.MISSES_TO_LOSE
        ).count()
        if lost_count >= Habit.LOST_HABITS_TO_BAN:
            user.is_active = False
            user.save(update_fields=["is_active"])
            banned.append(user)
            logger.warning("User %s banned: %s habits lost.", user.username, lost_count)

    banned_ids = {u.id for u in banned}
    return {
        "day": day,
        "misses": misses,
        # A habit lost on the day its owner is banned gets the ban notice instead.
        "lost": [h for h in lost if h.user_id not in banned_ids],
        "banned": banned,
    }
