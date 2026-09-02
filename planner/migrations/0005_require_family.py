import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0004_backfill_family'),
    ]

    operations = [
        migrations.AlterField(
            model_name='activity',
            name='family',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
        migrations.AlterField(
            model_name='customtask',
            name='family',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
        migrations.AlterField(
            model_name='familysettings',
            name='family',
            field=models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
        migrations.AlterField(
            model_name='groceryitem',
            name='family',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
        migrations.AlterField(
            model_name='recipe',
            name='family',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
        migrations.AlterField(
            model_name='taskcompletion',
            name='family',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
        migrations.AlterField(
            model_name='weeklymenuentry',
            name='family',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='planner.family'),
        ),
    ]
