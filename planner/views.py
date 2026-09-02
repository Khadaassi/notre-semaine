import datetime
import functools
import json

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .forms import SignUpForm, RecipeForm
from .models import (
    FamilySettings, Activity, TaskCompletion, Recipe, WeeklyMenuEntry, GroceryItem, CustomTask,
    FamilyMembership, PARENT_ROLES,
)
from .task_logic import (
    DAYS, DAY_FULL, tasks_for, next_day, pillar_for, is_zone_b_holiday, DEEP_CLEAN_ROOMS,
    group_by_phase,
)
from .default_data import DEFAULT_RECIPES, DEFAULT_GROCERY, DEFAULT_ACTIVITIES

PERSON_LABELS_STATIC = {'maman': 'Maman'}


@ratelimit(key='ip', rate='10/h', method='POST', block=True)
def signup(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            FamilyMembership.objects.create(user=user, family=form.matched_family)
            login(request, user)
            return redirect('today')
    else:
        form = SignUpForm()
    return render(request, 'registration/signup.html', {'form': form})


@method_decorator(ratelimit(key='ip', rate='10/h', method='POST', block=True), name='dispatch')
class RateLimitedLoginView(LoginView):
    template_name = 'registration/login.html'


def _get_family(request):
    return request.user.familymembership.family


def _is_parent(request):
    return request.user.familymembership.role in PARENT_ROLES


def parent_required(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not _is_parent(request):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapper


def _monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def _ensure_seed_data(family):
    if not Recipe.objects.filter(family=family).exists():
        for r in DEFAULT_RECIPES:
            Recipe.objects.create(family=family, **r)
    if not GroceryItem.objects.filter(family=family).exists():
        for cat, items in DEFAULT_GROCERY:
            for name in items:
                GroceryItem.objects.create(family=family, name=name, category=cat, is_default=True)
    if not Activity.objects.filter(family=family).exists():
        for a in DEFAULT_ACTIVITIES:
            Activity.objects.create(family=family, **a)


def _person_label(person, settings):
    return {
        'fille': settings.fille_name, 'fils': settings.fils_name,
        'maman': settings.maman_name, 'papa': settings.papa_name,
    }[person]


def _family_people(settings):
    kids = ['fille'] if settings.nb_enfants == 1 else ['fille', 'fils']
    return kids + ['maman', 'papa']


@login_required
def today(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))

    day = request.GET.get('day')
    today_idx = datetime.date.today().weekday()  # 0=lundi
    if day not in DAYS:
        day = DAYS[today_idx]

    real_date = datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))
    holiday_today = is_zone_b_holiday(real_date)
    holiday_tomorrow = is_zone_b_holiday(real_date + datetime.timedelta(days=1))

    people = _family_people(settings)
    is_parent = _is_parent(request)
    cards = []
    for person in people:
        task_list = tasks_for(person, day, settings, activities, holiday_today, holiday_tomorrow, custom_tasks)
        completions = {
            tc.task_id: tc.done
            for tc in TaskCompletion.objects.filter(family=family, person=person, date=real_date)
        }
        checkable = [x for x in task_list if not x['info']]
        done_count = sum(1 for x in checkable if completions.get(x['id']))
        pct = round(done_count / len(checkable) * 100) if checkable else 0
        for x in task_list:
            x['done'] = completions.get(x['id'], False)
        cards.append({
            'person': person,
            'name': _person_label(person, settings),
            'phases': group_by_phase(task_list),
            'pct': pct,
            'checkable_by_viewer': is_parent or person in ('fille', 'fils'),
        })

    day_chips = [{'key': d, 'label': DAY_FULL[d], 'full': DAY_FULL[d],
                  'is_today': i == today_idx, 'is_selected': d == day}
                 for i, d in enumerate(DAYS)]

    kid_cards = [c for c in cards if c['person'] in ('fille', 'fils')]
    parent_cards = [c for c in cards if c['person'] in ('maman', 'papa')]

    return render(request, 'planner/today.html', {
        'kid_cards': kid_cards, 'parent_cards': parent_cards, 'day': day, 'day_chips': day_chips,
        'real_date': real_date, 'settings': settings,
    })


@login_required
@require_POST
def toggle_task(request):
    family = _get_family(request)
    person = request.POST['person']
    if not _is_parent(request) and person not in ('fille', 'fils'):
        raise PermissionDenied
    task_id = request.POST['task_id']
    day = request.POST['day']
    done = request.POST['done'] == '1'
    today_idx = datetime.date.today().weekday()
    real_date = datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))
    TaskCompletion.objects.update_or_create(
        family=family, person=person, date=real_date, task_id=task_id, defaults={'done': done}
    )
    return JsonResponse({'ok': True})


def _day_type_label(d, holiday):
    if d in ('samedi', 'dimanche'):
        return 'Week-end'
    if holiday:
        return 'Vacances (écran OK)'
    if d == 'mercredi':
        return "Pas d'école"
    return 'École + devoirs'


def _maman_day_label(d, settings):
    if d in ('samedi', 'dimanche'):
        return 'Week-end'
    return 'Télétravail' if (d == 'mercredi' or d == settings.tt2_day) else 'Bureau'


# Tâches ménage qui reviennent tous les jours (rotation table/lave-vaisselle, cuisine) —
# exclues du tableau semainier pour ne garder que ce qui varie vraiment d'un jour à l'autre
# (deep clean, lessive, panier à linge...). Le détail complet reste dans la section du bas.
MENAGE_DAILY_IDS = {'lv_vide', 'rangertable_m', 'mettre_table', 'debarrasser_table', 'lv_remplit', 'cuisine'}

# Libellés courts pour le tableau semainier (les libellés complets de task_logic.py sont
# adaptés à une checklist, trop longs pour une cellule de tableau).
MENAGE_SHORT_LABELS = {
    'panierSDB': 'Panier linge', 'linge': 'Linge à plier', 'chambre': 'Chambre',
    'panier': 'Panier linge', 'frigo': 'Frigo', 'draps': 'Draps', 'reset': 'Reset général',
    'lessive': 'Lessive', 'menage': 'Rangement',
}


@login_required
def week_view(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    people = _family_people(settings)

    week_start = _monday_of(datetime.date.today())
    menu_by_day = {e.day: e.recipe for e in
                   WeeklyMenuEntry.objects.filter(family=family, week_start=week_start).select_related('recipe')}

    day_headers = [DAY_FULL[d][:3] for d in DAYS]
    ecole_cells, maman_cells, menage_cells, activites_cells, repas_cells = [], [], [], [], []

    for i, d in enumerate(DAYS):
        real_date = week_start + datetime.timedelta(days=i)
        holiday = is_zone_b_holiday(real_date)

        ecole_cells.append([_day_type_label(d, holiday)])
        maman_cells.append([_maman_day_label(d, settings)])

        by_label = {}
        for p in people:
            for x in tasks_for(p, d, settings, activities, holiday, False):
                if x['id'] in MENAGE_DAILY_IDS or pillar_for(x['id'], x['period']) != 'menage':
                    continue
                short = DEEP_CLEAN_ROOMS[d] if x['id'] == 'deepclean' else MENAGE_SHORT_LABELS.get(x['id'], x['label'])
                by_label.setdefault(short, []).append(_person_label(p, settings))
        menage_cells.append([f"{' & '.join(names)} : {label}" for label, names in by_label.items()])

        activites_cells.append([
            f"{_person_label(a.person, settings)} : {a.label}" + (f" ({a.time})" if a.time else '')
            for a in activities if a.day == d and a.person in people
        ])

        recipe = menu_by_day.get(d)
        repas_cells.append([recipe.name] if recipe else [])

    table_rows = [
        {'slot': 'École', 'icon': 'journee', 'cells': ecole_cells},
        {'slot': 'Maman', 'icon': 'travail', 'cells': maman_cells},
        {'slot': 'Ménage', 'icon': 'menage', 'cells': menage_cells},
        {'slot': 'Activités', 'icon': 'activite', 'cells': activites_cells},
        {'slot': 'Repas du soir', 'icon': 'repas', 'cells': repas_cells},
    ]
    rotation_note = (f"Table → {_person_label(settings.rotation_table, settings)} · "
                      f"Lave-vaisselle → {_person_label(settings.rotation_lave_vaisselle, settings)}")

    return render(request, 'planner/week.html', {
        'day_headers': day_headers, 'table_rows': table_rows, 'rotation_note': rotation_note,
    })


@login_required
def maison(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    items = GroceryItem.objects.filter(family=family)
    grouped = {}
    for i in items:
        grouped.setdefault(i.category or 'Ajoutés', []).append(i)
    return render(request, 'planner/maison.html', {
        'settings': settings, 'grouped': grouped,
    })


@login_required
@require_POST
def toggle_grocery(request):
    family = _get_family(request)
    item = GroceryItem.objects.get(pk=request.POST['item_id'], family=family)
    item.checked = request.POST['checked'] == '1'
    item.save()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def add_grocery(request):
    family = _get_family(request)
    name = request.POST.get('name', '').strip()
    if name:
        GroceryItem.objects.get_or_create(family=family, name=name, defaults={'category': 'Ajoutés'})
        messages.success(request, "Article ajouté à la liste de courses.")
    return redirect('maison')


@login_required
@require_POST
def reset_grocery(request):
    family = _get_family(request)
    GroceryItem.objects.filter(family=family).update(checked=False)
    messages.success(request, "Liste de courses réinitialisée.")
    return redirect('maison')


@login_required
@parent_required
@require_POST
def toggle_rotation(request):
    family = _get_family(request)
    which = request.POST['which']  # 'table' or 'lv'
    settings = FamilySettings.load(family)
    if which == 'table':
        settings.rotation_table = 'fils' if settings.rotation_table == 'fille' else 'fille'
    else:
        settings.rotation_lave_vaisselle = 'fils' if settings.rotation_lave_vaisselle == 'fille' else 'fille'
    settings.save()
    return redirect('maison')


@login_required
def menu(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    week_start = _monday_of(datetime.date.today())
    recipes = Recipe.objects.filter(family=family)
    by_cat = {}
    for r in recipes:
        by_cat.setdefault(r.category, []).append(r)

    entries = {e.day: e.recipe_id for e in WeeklyMenuEntry.objects.filter(family=family, week_start=week_start)}
    chosen_ids = [v for v in entries.values() if v]
    chosen_recipes = Recipe.objects.filter(family=family, id__in=chosen_ids)
    all_ingredients = sorted({ing for r in chosen_recipes for ing in r.ingredients})

    if request.method == 'POST':
        if 'add_recipe' in request.POST:
            form = RecipeForm(request.POST, request.FILES)
            if form.is_valid():
                recipe = form.save(commit=False)
                recipe.family = family
                recipe.save()
                messages.success(request, "Recette enregistrée.")
            return redirect('menu')
        if 'delete_recipe' in request.POST:
            Recipe.objects.filter(id=request.POST['delete_recipe'], family=family).delete()
            messages.success(request, "Recette supprimée.")
            return redirect('menu')
        if 'set_day' in request.POST:
            day = request.POST['set_day']
            recipe_id = request.POST.get('recipe_id') or None
            if recipe_id and not Recipe.objects.filter(id=recipe_id, family=family).exists():
                messages.error(request, "Recette invalide.")
                return redirect('menu')
            WeeklyMenuEntry.objects.update_or_create(
                family=family, week_start=week_start, day=day, defaults={'recipe_id': recipe_id}
            )
            return redirect('menu')
        if 'copy_to_courses' in request.POST:
            for ing in all_ingredients:
                GroceryItem.objects.get_or_create(family=family, name=ing, defaults={'category': 'Menu de la semaine'})
            messages.success(request, "Ingrédients ajoutés à la liste de courses.")
            return redirect('menu')

    recipe_form = RecipeForm()
    day_rows = [{'day': d, 'label': DAY_FULL[d], 'selected': entries.get(d)} for d in DAYS]

    return render(request, 'planner/menu.html', {
        'by_cat': by_cat, 'day_rows': day_rows, 'recipes': recipes,
        'all_ingredients': all_ingredients, 'recipe_form': recipe_form,
    })


@login_required
@parent_required
def settings_view(request):
    family = _get_family(request)
    settings = FamilySettings.load(family)
    if request.method == 'POST':
        if 'add_activity' in request.POST:
            label = request.POST.get('act_label', '').strip()
            if label:
                Activity.objects.create(
                    family=family,
                    person=request.POST.get('act_person', 'fils'),
                    label=label,
                    day=request.POST.get('act_day', 'lundi'),
                    time=request.POST.get('act_time', '').strip(),
                )
                messages.success(request, "Activité ajoutée.")
        elif 'add_custom_task' in request.POST:
            label = request.POST.get('task_label', '').strip()
            if label:
                CustomTask.objects.create(
                    family=family,
                    person=request.POST.get('task_person', 'fille'),
                    day=request.POST.get('task_day', 'lundi'),
                    period=request.POST.get('task_period', 'matin'),
                    label=label,
                )
                messages.success(request, "Tâche ajoutée.")
        else:
            settings.maman_name = request.POST.get('maman_name', settings.maman_name).strip() or settings.maman_name
            settings.fille_name = request.POST.get('fille_name', settings.fille_name).strip() or settings.fille_name
            settings.papa_name = request.POST.get('papa_name', settings.papa_name).strip() or settings.papa_name
            settings.nb_enfants = int(request.POST.get('nb_enfants', settings.nb_enfants))
            if settings.nb_enfants == 1:
                settings.rotation_table = 'fille'
                settings.rotation_lave_vaisselle = 'fille'
            else:
                settings.fils_name = request.POST.get('fils_name', settings.fils_name).strip() or settings.fils_name
            settings.maman_travaille = 'maman_travaille' in request.POST
            settings.tt2_day = request.POST.get('tt2_day', settings.tt2_day)
            settings.courses_day = request.POST.get('courses_day', settings.courses_day)
            settings.papa_travaille = 'papa_travaille' in request.POST
            settings.week_note = request.POST.get('week_note', '')
            settings.save()
            messages.success(request, "Réglages enregistrés.")
        return redirect('settings')

    activities = list(Activity.objects.filter(family=family))
    for a in activities:
        a.person_name = _person_label(a.person, settings)
    custom_tasks = list(CustomTask.objects.filter(family=family))
    for c in custom_tasks:
        c.person_name = _person_label(c.person, settings)
    members = FamilyMembership.objects.filter(family=family).select_related('user')
    return render(request, 'planner/settings.html', {
        'settings': settings, 'activities': activities, 'custom_tasks': custom_tasks,
        'days': DAYS, 'day_full': DAY_FULL, 'members': members,
    })


@login_required
@parent_required
@require_POST
def remove_member(request, pk):
    family = _get_family(request)
    membership = FamilyMembership.objects.filter(pk=pk, family=family).exclude(user=request.user).first()
    if membership:
        membership.delete()
        messages.success(request, "Membre retiré de la famille.")
    else:
        messages.error(request, "Action impossible.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def delete_activity(request, pk):
    Activity.objects.filter(pk=pk, family=_get_family(request)).delete()
    messages.success(request, "Activité supprimée.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def delete_custom_task(request, pk):
    CustomTask.objects.filter(pk=pk, family=_get_family(request)).delete()
    messages.success(request, "Tâche supprimée.")
    return redirect('settings')
