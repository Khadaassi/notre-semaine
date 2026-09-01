import datetime
import json

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from .forms import SignUpForm, RecipeForm
from .models import FamilySettings, Activity, TaskCompletion, Recipe, WeeklyMenuEntry, GroceryItem
from .task_logic import DAYS, DAY_FULL, tasks_for, next_day
from .default_data import DEFAULT_RECIPES, DEFAULT_GROCERY, DEFAULT_ACTIVITIES

PERSON_LABELS_STATIC = {'maman': 'Maman'}


def signup(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('today')
    else:
        form = SignUpForm()
    return render(request, 'registration/signup.html', {'form': form})


def _monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def _ensure_seed_data():
    if not Recipe.objects.exists():
        for r in DEFAULT_RECIPES:
            Recipe.objects.create(**r)
    if not GroceryItem.objects.exists():
        for cat, items in DEFAULT_GROCERY:
            for name in items:
                GroceryItem.objects.create(name=name, category=cat, is_default=True)
    if not Activity.objects.exists():
        for a in DEFAULT_ACTIVITIES:
            Activity.objects.create(**a)


def _person_label(person, settings):
    return {
        'fille': settings.fille_name, 'fils': settings.fils_name,
        'maman': 'Maman', 'papa': settings.papa_name,
    }[person]


@login_required
def today(request):
    _ensure_seed_data()
    settings = FamilySettings.load()
    activities = list(Activity.objects.all())

    day = request.GET.get('day')
    today_idx = datetime.date.today().weekday()  # 0=lundi
    if day not in DAYS:
        day = DAYS[today_idx]

    real_date = datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))

    people = ['fille', 'fils', 'maman', 'papa']
    cards = []
    for person in people:
        task_list = tasks_for(person, day, settings, activities)
        completions = {
            tc.task_id: tc.done
            for tc in TaskCompletion.objects.filter(person=person, date=real_date)
        }
        checkable = [x for x in task_list if not x['info']]
        done_count = sum(1 for x in checkable if completions.get(x['id']))
        pct = round(done_count / len(checkable) * 100) if checkable else 0
        for x in task_list:
            x['done'] = completions.get(x['id'], False)
        cards.append({
            'person': person,
            'name': _person_label(person, settings),
            'tasks': task_list,
            'pct': pct,
        })

    day_chips = [{'key': d, 'label': DAY_FULL[d], 'full': DAY_FULL[d],
                  'is_today': i == today_idx, 'is_selected': d == day}
                 for i, d in enumerate(DAYS)]

    return render(request, 'planner/today.html', {
        'cards': cards, 'day': day, 'day_chips': day_chips,
        'real_date': real_date, 'settings': settings,
    })


@login_required
@require_POST
def toggle_task(request):
    person = request.POST['person']
    task_id = request.POST['task_id']
    day = request.POST['day']
    done = request.POST['done'] == '1'
    today_idx = datetime.date.today().weekday()
    real_date = datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))
    TaskCompletion.objects.update_or_create(
        person=person, date=real_date, task_id=task_id, defaults={'done': done}
    )
    return JsonResponse({'ok': True})


@login_required
def week_view(request):
    settings = FamilySettings.load()
    rows_def = [
        ('7h00', lambda d: 'Réveil libre' if d in ('samedi', 'dimanche') else 'Réveil — petit-déj'),
        ('Matin', lambda d: 'Rangement, linge' if d == 'samedi' else (
            "Cours d'arabe" if d == 'dimanche' else ("Pas d'école" if d == 'mercredi' else 'École 8h30'))),
        ('Journée / Maman', lambda d: (
            (('Courses + activité famille' if settings.courses_day == 'samedi' else 'Activité famille') if d == 'samedi'
             else ('Batch cooking + lessive' if d == 'dimanche'
                   else ('Télétravail 9h–17h' if (d == 'mercredi' or d == settings.tt2_day) else 'Bureau 9h–17h'))))),
        ('16h30', lambda d: '—' if d in ('samedi', 'dimanche') else (
            'Journée libre avec les enfants' if d == 'mercredi' else 'Retour maison — goûter')),
        ("Fin d'après-midi", lambda d: 'Chez Mahlo' if d == 'dimanche' else '—'),
        ('Soir', lambda d: _soir_summary(d, settings)),
        ('20h30', lambda d: 'Coucher souple' if d in ('samedi', 'dimanche') else 'Coucher'),
    ]
    table = [{'slot': slot, 'cells': [fn(d) for d in DAYS]} for slot, fn in rows_def]
    day_headers = [DAY_FULL[d][:3] for d in DAYS]
    return render(request, 'planner/week.html', {'table': table, 'days': DAYS, 'day_headers': day_headers})


def _soir_summary(d, settings):
    if d == 'samedi':
        return 'Activité famille'
    if d == 'dimanche':
        return 'Repos'
    s = 'Coran (~1h)' if d == 'mercredi' else 'Devoirs · Coran (~1h)'
    acts = list(Activity.objects.filter(day=d))
    if acts:
        s += ' · ' + ', '.join(a.label + (f' {a.time}' if a.time else '') for a in acts)
    return s


@login_required
def maison(request):
    _ensure_seed_data()
    settings = FamilySettings.load()
    items = GroceryItem.objects.all()
    grouped = {}
    for i in items:
        grouped.setdefault(i.category or 'Ajoutés', []).append(i)
    return render(request, 'planner/maison.html', {
        'settings': settings, 'grouped': grouped,
    })


@login_required
@require_POST
def toggle_grocery(request):
    item = GroceryItem.objects.get(pk=request.POST['item_id'])
    item.checked = request.POST['checked'] == '1'
    item.save()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def add_grocery(request):
    name = request.POST.get('name', '').strip()
    if name:
        GroceryItem.objects.get_or_create(name=name, defaults={'category': 'Ajoutés'})
    return redirect('maison')


@login_required
@require_POST
def reset_grocery(request):
    GroceryItem.objects.update(checked=False)
    return redirect('maison')


@login_required
@require_POST
def toggle_rotation(request):
    which = request.POST['which']  # 'table' or 'lv'
    settings = FamilySettings.load()
    if which == 'table':
        settings.rotation_table = 'fils' if settings.rotation_table == 'fille' else 'fille'
    else:
        settings.rotation_lave_vaisselle = 'fils' if settings.rotation_lave_vaisselle == 'fille' else 'fille'
    settings.save()
    return redirect('maison')


@login_required
def menu(request):
    _ensure_seed_data()
    week_start = _monday_of(datetime.date.today())
    recipes = Recipe.objects.all()
    by_cat = {}
    for r in recipes:
        by_cat.setdefault(r.category, []).append(r)

    entries = {e.day: e.recipe_id for e in WeeklyMenuEntry.objects.filter(week_start=week_start)}
    chosen_ids = [v for v in entries.values() if v]
    chosen_recipes = Recipe.objects.filter(id__in=chosen_ids)
    all_ingredients = sorted({ing for r in chosen_recipes for ing in r.ingredients})

    if request.method == 'POST':
        if 'add_recipe' in request.POST:
            form = RecipeForm(request.POST, request.FILES)
            if form.is_valid():
                form.save()
            return redirect('menu')
        if 'delete_recipe' in request.POST:
            Recipe.objects.filter(id=request.POST['delete_recipe']).delete()
            return redirect('menu')
        if 'set_day' in request.POST:
            day = request.POST['set_day']
            recipe_id = request.POST.get('recipe_id') or None
            WeeklyMenuEntry.objects.update_or_create(
                week_start=week_start, day=day, defaults={'recipe_id': recipe_id}
            )
            return redirect('menu')
        if 'copy_to_courses' in request.POST:
            for ing in all_ingredients:
                GroceryItem.objects.get_or_create(name=ing, defaults={'category': 'Menu de la semaine'})
            return redirect('menu')

    recipe_form = RecipeForm()
    day_rows = [{'day': d, 'label': DAY_FULL[d], 'selected': entries.get(d)} for d in DAYS]

    return render(request, 'planner/menu.html', {
        'by_cat': by_cat, 'day_rows': day_rows, 'recipes': recipes,
        'all_ingredients': all_ingredients, 'recipe_form': recipe_form,
    })


@login_required
def settings_view(request):
    settings = FamilySettings.load()
    if request.method == 'POST':
        settings.tt2_day = request.POST.get('tt2_day', settings.tt2_day)
        settings.courses_day = request.POST.get('courses_day', settings.courses_day)
        settings.papa_travaille = 'papa_travaille' in request.POST
        settings.week_note = request.POST.get('week_note', '')
        settings.save()

        if 'add_activity' in request.POST:
            label = request.POST.get('act_label', '').strip()
            if label:
                Activity.objects.create(
                    person=request.POST.get('act_person', 'fils'),
                    label=label,
                    day=request.POST.get('act_day', 'lundi'),
                    time=request.POST.get('act_time', '').strip(),
                )
        return redirect('settings')

    activities = Activity.objects.all()
    return render(request, 'planner/settings.html', {'settings': settings, 'activities': activities, 'days': DAYS,
                                                       'day_full': DAY_FULL})


@login_required
@require_POST
def delete_activity(request, pk):
    Activity.objects.filter(pk=pk).delete()
    return redirect('settings')
