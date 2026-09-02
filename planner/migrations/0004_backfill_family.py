from django.conf import settings as django_settings
from django.db import migrations


def backfill(apps, schema_editor):
    Family = apps.get_model('planner', 'Family')
    FamilyMembership = apps.get_model('planner', 'FamilyMembership')
    User = apps.get_model('auth', 'User')
    FamilySettings = apps.get_model('planner', 'FamilySettings')
    Activity = apps.get_model('planner', 'Activity')
    TaskCompletion = apps.get_model('planner', 'TaskCompletion')
    Recipe = apps.get_model('planner', 'Recipe')
    WeeklyMenuEntry = apps.get_model('planner', 'WeeklyMenuEntry')
    CustomTask = apps.get_model('planner', 'CustomTask')
    GroceryItem = apps.get_model('planner', 'GroceryItem')

    if not (FamilySettings.objects.exists() or Activity.objects.exists() or User.objects.exists()):
        return  # fresh database, nothing to backfill

    family, _ = Family.objects.get_or_create(
        invite_code=getattr(django_settings, 'FAMILY_INVITE_CODE', 'REPLACE_ME'),
        defaults={'name': 'Notre famille'},
    )

    for user in User.objects.all():
        FamilyMembership.objects.get_or_create(user=user, defaults={'family': family})

    for model in (FamilySettings, Activity, TaskCompletion, Recipe, WeeklyMenuEntry, CustomTask, GroceryItem):
        model.objects.filter(family__isnull=True).update(family=family)


def noop_reverse(apps, schema_editor):
    pass  # data migration — nothing meaningful to reverse


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0003_family_alter_taskcompletion_unique_together_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, noop_reverse),
    ]
