# Generated manually (data migration) on 2026-09-16

from django.db import migrations


def populate_days_from_day(apps, schema_editor):
    CustomTask = apps.get_model('planner', 'CustomTask')
    for task in CustomTask.objects.exclude(day='').iterator():
        task.days = [task.day]
        task.save(update_fields=['days'])


def revert_days_to_day(apps, schema_editor):
    """Best-effort reverse: only the first day of a (possibly now multi-day) task can be
    represented in the old single-value field."""
    CustomTask = apps.get_model('planner', 'CustomTask')
    for task in CustomTask.objects.iterator():
        task.day = task.days[0] if task.days else ''
        task.save(update_fields=['day'])


class Migration(migrations.Migration):
    """Step 2/3 of the CustomTask day -> days conversion: for every existing row, copy the
    single `day` value into the new `days` list (e.g. day='lundi' -> days=['lundi']). Runs
    before 0017_customtask_remove_day drops the old column, so no data is lost."""

    dependencies = [
        ('planner', '0015_customtask_add_days'),
    ]

    operations = [
        migrations.RunPython(populate_days_from_day, revert_days_to_day),
    ]
