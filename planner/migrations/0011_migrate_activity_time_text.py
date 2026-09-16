import re

from django.db import migrations

# Best-effort parse of the old freeform `time` text (e.g. "17h30", "17:30", "9h", "vers 18h")
# into an (hour, minute) pair, so existing activity times survive the move to a real
# TimeField. Text that doesn't contain a recognizable time is left unset rather than
# guessed at.
_TIME_RE = re.compile(r'(\d{1,2})\s*[h:]\s*(\d{2})?')


def _parse_time(text):
    if not text:
        return None
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def migrate_time_text_forward(apps, schema_editor):
    Activity = apps.get_model('planner', 'Activity')
    for activity in Activity.objects.exclude(time=''):
        parsed = _parse_time(activity.time)
        if parsed:
            import datetime
            activity.start_time = datetime.time(*parsed)
            activity.save(update_fields=['start_time'])


def migrate_time_text_backward(apps, schema_editor):
    # The old `time` field is reintroduced empty by the preceding migration's reversal;
    # there's no lossless way to re-derive its original free text from a TimeField, so
    # this is a no-op on the way down.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0010_activity_add_v2_fields'),
    ]

    operations = [
        migrations.RunPython(migrate_time_text_forward, migrate_time_text_backward),
    ]
