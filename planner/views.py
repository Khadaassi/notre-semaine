import datetime
import functools
import json

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .forms import SignUpForm, RecipeForm
from .models import (
    Family, FamilySettings, Activity, TaskCompletion, Recipe, WeeklyMenuEntry, GroceryItem,
    CustomTask, FamilyMembership, PARENT_ROLES, TaskOrder, StarAward, KidStars,
)
from .task_logic import (
    DAYS, DAY_FULL, tasks_for, next_day, pillar_for, is_zone_b_holiday, DEEP_CLEAN_ROOMS,
    group_by_phase, apply_order, parse_free_time,
)
from .default_data import DEFAULT_RECIPES, DEFAULT_GROCERY, DEFAULT_ACTIVITIES

PERSON_LABELS_STATIC = {'maman': 'Maman'}
STAR_MILESTONE = 15  # every Nth fully-completed day surfaces the surprise reward popup
STARS_PER_LEVEL = STAR_MILESTONE * 6  # cosmetic "Niv." badge — one level per 6 surprises (~12 weeks)


def _level_for(total_stars):
    return total_stars // STARS_PER_LEVEL + 1


@ratelimit(key='ip', rate='10/h', method='POST', block=True)
def signup(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                # Lock the family row so two concurrent signups can't both see "no parent
                # yet" and both get auto-promoted.
                family = Family.objects.select_for_update().get(pk=form.matched_family.pk)
                is_first_member = not FamilyMembership.objects.filter(family=family).exists()
                role = 'maman' if is_first_member else 'enfants'
                user = form.save()
                FamilyMembership.objects.create(user=user, family=family, role=role)
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


def _real_date_for_day(day):
    today_idx = datetime.date.today().weekday()
    return datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))


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
    orders = {}
    for o in TaskOrder.objects.filter(family=family):
        orders.setdefault(o.person, {})[o.task_id] = o.order
    levels = {s.person: _level_for(s.total) for s in KidStars.objects.filter(family=family)}

    cards = []
    for person in people:
        task_list = tasks_for(person, day, settings, activities, holiday_today, holiday_tomorrow, custom_tasks)
        completions = {
            tc.task_id: tc
            for tc in TaskCompletion.objects.filter(family=family, person=person, date=real_date)
        }
        for x in task_list:
            tc = completions.get(x['id'])
            x['done'] = tc.done if tc else False
            x['seconds_spent'] = tc.seconds_spent if tc else 0
            x['timer_running'] = bool(tc and tc.timer_started_at)
            x['timer_started_ms'] = int(tc.timer_started_at.timestamp() * 1000) if (tc and tc.timer_started_at) else None
        phases = [(pk, pl, apply_order(ts, orders.get(person, {}))) for pk, pl, ts in group_by_phase(task_list)]
        phase_cards = []
        for phase_key, phase_label, tasks in phases:
            checkable = [x for x in tasks if not x['info']]
            done_count = sum(1 for x in checkable if x['done'])
            phase_cards.append({
                'phase_key': phase_key,
                'phase_label': phase_label,
                'tasks': tasks,
                'pct': round(done_count / len(checkable) * 100) if checkable else 0,
            })
        cards.append({
            'person': person,
            'name': _person_label(person, settings),
            'phase_cards': phase_cards,
            'checkable_by_viewer': is_parent or person in ('fille', 'fils'),
            'level': levels.get(person, 1) if person in ('fille', 'fils') else None,
        })

    day_chips = [{'key': d, 'label': DAY_FULL[d], 'full': DAY_FULL[d],
                  'is_today': i == today_idx, 'is_selected': d == day}
                 for i, d in enumerate(DAYS)]

    kid_cards = [c for c in cards if c['person'] in ('fille', 'fils')]
    parent_cards = [c for c in cards if c['person'] in ('maman', 'papa')] if is_parent else []

    return render(request, 'planner/today.html', {
        'kid_cards': kid_cards, 'parent_cards': parent_cards, 'day': day, 'day_chips': day_chips,
        'real_date': real_date, 'settings': settings,
    })


def _checkable_ids_for(person, day, family):
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))
    real_date = _real_date_for_day(day)
    holiday_today = is_zone_b_holiday(real_date)
    holiday_tomorrow = is_zone_b_holiday(real_date + datetime.timedelta(days=1))
    task_list = tasks_for(person, day, settings, activities, holiday_today, holiday_tomorrow, custom_tasks)
    return {x['id'] for x in task_list if not x['info']}


def _award_star_if_day_complete(family, person, day, real_date):
    """Called after a kid's task is checked off. If that completes every checkable task for
    the day, silently banks one star (see StarAward/KidStars) and reports whether this star
    just crossed a STAR_MILESTONE threshold — the one moment the kid actually sees anything."""
    checkable_ids = _checkable_ids_for(person, day, family)
    if not checkable_ids:
        return False, None
    done_ids = set(TaskCompletion.objects.filter(
        family=family, person=person, date=real_date, done=True
    ).values_list('task_id', flat=True))
    if not checkable_ids.issubset(done_ids):
        return False, None
    _, created = StarAward.objects.get_or_create(family=family, person=person, date=real_date)
    if not created:
        return False, None
    stars, _ = KidStars.objects.get_or_create(family=family, person=person)
    stars.total += 1
    reached = stars.total // STAR_MILESTONE
    milestone_reached = reached > stars.milestones_shown
    if milestone_reached:
        stars.milestones_shown = reached
    stars.save()
    return milestone_reached, stars.total


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
    real_date = _real_date_for_day(day)
    TaskCompletion.objects.update_or_create(
        family=family, person=person, date=real_date, task_id=task_id, defaults={'done': done}
    )
    milestone_reached, stars_total = False, None
    if done and person in ('fille', 'fils'):
        milestone_reached, stars_total = _award_star_if_day_complete(family, person, day, real_date)
    return JsonResponse({'ok': True, 'milestone_reached': milestone_reached, 'stars_total': stars_total})


@login_required
@require_POST
def timer_task(request):
    family = _get_family(request)
    person = request.POST['person']
    if not _is_parent(request) and person not in ('fille', 'fils'):
        raise PermissionDenied
    task_id = request.POST['task_id']
    day = request.POST['day']
    action = request.POST.get('action')
    if day not in DAYS or action not in ('start', 'stop'):
        return JsonResponse({'ok': False}, status=400)
    real_date = _real_date_for_day(day)
    tc, _ = TaskCompletion.objects.get_or_create(family=family, person=person, date=real_date, task_id=task_id)
    now = timezone.now()
    if action == 'start' and not tc.timer_started_at:
        tc.timer_started_at = now
        tc.save(update_fields=['timer_started_at'])
    elif action == 'stop' and tc.timer_started_at:
        elapsed = max(0, int((now - tc.timer_started_at).total_seconds()))
        tc.seconds_spent += elapsed
        tc.timer_started_at = None
        tc.save(update_fields=['seconds_spent', 'timer_started_at'])
    return JsonResponse({
        'ok': True,
        'seconds_spent': tc.seconds_spent,
        'running': tc.timer_started_at is not None,
        'started_at_ms': int(tc.timer_started_at.timestamp() * 1000) if tc.timer_started_at else None,
    })


@login_required
@require_POST
def reorder_tasks(request):
    family = _get_family(request)
    person = request.POST.get('person')
    if not _is_parent(request) and person not in ('fille', 'fils'):
        raise PermissionDenied
    task_ids = [tid for tid in request.POST.getlist('task_ids[]') if tid]
    for idx, task_id in enumerate(task_ids):
        TaskOrder.objects.update_or_create(
            family=family, person=person, task_id=task_id, defaults={'order': idx}
        )
    return JsonResponse({'ok': True})


@login_required
def stars_view(request):
    family = _get_family(request)
    settings = FamilySettings.load(family)
    kids = [p for p in _family_people(settings) if p in ('fille', 'fils')]
    today = datetime.date.today()
    days = [today - datetime.timedelta(days=i) for i in range(27, -1, -1)]

    trackers = []
    for kid in kids:
        stars, _ = KidStars.objects.get_or_create(family=family, person=kid)
        awarded_dates = set(StarAward.objects.filter(
            family=family, person=kid, date__gte=days[0]
        ).values_list('date', flat=True))
        streak = 0
        cursor = today
        while cursor in awarded_dates:
            streak += 1
            cursor -= datetime.timedelta(days=1)
        trackers.append({
            'person': kid,
            'name': _person_label(kid, settings),
            'total': stars.total,
            'level': _level_for(stars.total),
            'in_cycle': stars.total % STAR_MILESTONE,
            'milestone': STAR_MILESTONE,
            'streak': streak,
            'days': [{'date': d, 'filled': d in awarded_dates, 'is_today': d == today} for d in days],
        })

    return render(request, 'planner/stars.html', {'trackers': trackers})


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
            f"{_person_label(a.person, settings)} : {a.label}" + (f" ({a.time_range_label()})" if a.time_range_label() else '')
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
                    start_time=parse_free_time(request.POST.get('act_time', '')),
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
def promote_member(request, pk):
    """Grants an existing family member a parent role. Only a current parent can do this —
    the invite code itself only ever grants the 'enfants' role (see views.signup)."""
    family = _get_family(request)
    role = request.POST.get('role', 'maman')
    if role not in PARENT_ROLES:
        messages.error(request, "Rôle invalide.")
        return redirect('settings')
    membership = FamilyMembership.objects.filter(pk=pk, family=family).first()
    if membership:
        membership.role = role
        membership.save(update_fields=['role'])
        messages.success(request, "Membre promu au rôle parent.")
    else:
        messages.error(request, "Action impossible.")
    return redirect('settings')


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
