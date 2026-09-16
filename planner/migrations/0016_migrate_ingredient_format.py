from django.db import migrations


def convert_ingredients_forward(apps, schema_editor):
    """Lot 4: Recipe.ingredients moves from a plain list of name strings to a
    list of {"name", "quantity", "unit"} dicts. Existing rows (including real
    recipes already in prod) are converted here; quantity/unit are left
    None/'' since the old format never recorded them. Idempotent — re-running
    on already-migrated rows is a no-op."""
    Recipe = apps.get_model('planner', 'Recipe')
    for recipe in Recipe.objects.all():
        raw = recipe.ingredients or []
        normalized = []
        changed = False
        for entry in raw:
            if isinstance(entry, str):
                name = entry.strip()
                if name:
                    normalized.append({'name': name, 'quantity': None, 'unit': ''})
                changed = True
            elif isinstance(entry, dict):
                name = (entry.get('name') or '').strip()
                if name:
                    normalized.append({
                        'name': name,
                        'quantity': entry.get('quantity'),
                        'unit': (entry.get('unit') or '').strip(),
                    })
                else:
                    changed = True
            else:
                # Unexpected shape — drop it defensively rather than fail the migration.
                changed = True
        if changed:
            recipe.ingredients = normalized
            recipe.save(update_fields=['ingredients'])


def convert_ingredients_backward(apps, schema_editor):
    """Reverse: collapses the structured dicts back to a plain name list
    (quantity/unit are lost — this is only meant to unblock a schema
    rollback, not to be used routinely)."""
    Recipe = apps.get_model('planner', 'Recipe')
    for recipe in Recipe.objects.all():
        raw = recipe.ingredients or []
        names = []
        for entry in raw:
            if isinstance(entry, dict):
                name = (entry.get('name') or '').strip()
                if name:
                    names.append(name)
            elif isinstance(entry, str) and entry.strip():
                names.append(entry.strip())
        recipe.ingredients = names
        recipe.save(update_fields=['ingredients'])


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0015_groceryitem_already_home_groceryitem_quantity_and_more'),
    ]

    operations = [
        migrations.RunPython(convert_ingredients_forward, convert_ingredients_backward),
    ]
