import datetime
import functools
import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .forms import SignUpForm, RecipeForm
from .models import (
    FamilySettings, Activity, TaskCompletion, Recipe, WeeklyMenuEntry, GroceryItem,
    CustomTask, FamilyMembership, PARENT_ROLES, TaskOrder, StarAward, KidStars, TaskException,
    format_quantity, TASK_EXCEPTION_KIND_CHOICES, DayMode, DAY_MODE_CHOICES, PERSON_CHOICES,
)
from .task_logic import (
    DAYS, DAY_FULL, tasks_for, next_day, pillar_for, is_zone_b_holiday, DEEP_CLEAN_ROOMS,
    group_by_phase, apply_order, parse_free_time, split_by_exceptions, find_schedule_conflicts,
    active_day_mode,
)
from .default_data import DEFAULT_RECIPES, DEFAULT_GROCERY, DEFAULT_ACTIVITIES

PERSON_KEYS = {p for p, _ in PERSON_CHOICES}

PERSON_LABELS_STATIC = {'maman': 'Maman'}
MENU_GROCERY_CATEGORY = 'Menu de la semaine'  # category tag used by copy_to_courses (see menu())


def _level_for(total_stars, milestone):
    """Cosmetic "Niv." badge — one level per 6 surprises, scaled to that family's own
    star_milestone (see FamilySettings.star_milestone) instead of a fixed constant."""
    stars_per_level = max(1, milestone) * 6
    return total_stars // stars_per_level + 1


@ratelimit(key='ip', rate='10/h', method='POST', block=True)
def signup(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                family = form.matched_family
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


def _can_act_on(request, person):
    """True if the signed-in account may check off / time / reorder a task belonging to
    `person`. A parent may act on anyone. An 'enfants' account may act only on the one kid
    a parent has assigned it to via FamilyMembership.kid_person — never a sibling's tasks,
    and never before it's been assigned (see views.today for the matching read-only fallback
    on the page itself, and settings_view for how a parent assigns it)."""
    membership = request.user.familymembership
    if membership.role in PARENT_ROLES:
        return True
    return bool(membership.kid_person) and person == membership.kid_person


def _monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def _real_date_for_day(day):
    today_idx = datetime.date.today().weekday()
    return datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))


def _current_phase_now(now_time):
    """Maps a wall-clock time to one of the 3 accordion phases used on 'Aujourd'hui'
    (see group_by_phase in task_logic.py): before 12h00 = matin, 12h00–18h00 = journée,
    18h00 onward = soir. A simple, deliberately time-of-day-only split — it has no relation
    to any one family member's actual school/work hours (which vary by day and person, see
    task_logic.is_bureau_day etc.) and isn't meant to be precise, just a reasonable default
    for which accordion panel opens automatically."""
    if now_time < datetime.time(12, 0):
        return 'matin'
    if now_time < datetime.time(18, 0):
        return 'journee'
    return 'soir'


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


def _kids_people(settings):
    return ['fille'] if settings.nb_enfants == 1 else ['fille', 'fils']


def _family_people(settings):
    return _kids_people(settings) + ['maman', 'papa']


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

    # Only today's date has a meaningful "current period" — on any other day chip every
    # phase card just opens expanded (see 'open_default' below).
    is_today_view = day == DAYS[today_idx]
    now = datetime.datetime.now()
    current_phase = _current_phase_now(now.time())

    # Direct access to the evening meal — unlike "À venir"/"À préparer pour demain" this isn't
    # relative to right now, so it's shown for whichever day chip is selected, not just today.
    week_start = _monday_of(datetime.date.today())
    menu_entry = WeeklyMenuEntry.objects.filter(
        family=family, week_start=week_start, day=day
    ).select_related('recipe').first()
    tonight_recipe = menu_entry.recipe if menu_entry else None

    membership = request.user.familymembership
    is_parent = membership.role in PARENT_ROLES
    # An 'enfants' account only ever gets to see/act on the one kid_person a parent has
    # assigned it to (FamilyMembership.kid_person) — never a sibling's tasks, and never a
    # parent's. Until it's assigned, it falls back to read-only access to every kid's card
    # (see checkable_by_viewer below) rather than an empty or broken page — a parent assigns
    # it from the member list in Réglages (settings_view / set_member_kid).
    kids_people = _kids_people(settings)
    if is_parent:
        view_people = _family_people(settings)
    elif membership.kid_person:
        view_people = [membership.kid_person]
    else:
        view_people = kids_people

    # "Toute la famille / Moi / chaque enfant" selector (?who=), parent-only: an 'enfants'
    # account already only ever sees the single card view_people resolved to above, so there's
    # nothing left for it to filter — the selector isn't offered to it (who_options stays None,
    # see today.html). "Moi" maps to the viewer's own person: their role for a parent
    # (role is literally 'maman'/'papa', the same string as the person key).
    who_options, who = None, None
    if is_parent:
        who = request.GET.get('who', 'all')
        valid_who = {'all', 'me'} | set(kids_people)
        if who not in valid_who:
            who = 'all'
        if who == 'me':
            view_people = [membership.role]
        elif who != 'all':
            view_people = [who]
        who_options = [
            {'key': 'all', 'label': 'Toute la famille', 'selected': who == 'all'},
            {'key': 'me', 'label': 'Moi', 'selected': who == 'me'},
        ] + [
            {'key': kid, 'label': _person_label(kid, settings), 'selected': who == kid}
            for kid in kids_people
        ]

    # "À venir" — the next not-yet-started Activity today, scoped to the same people the
    # who-filter above resolved to (so a kid account only ever sees its own upcoming activity,
    # and a parent's "Moi"/per-kid filter narrows this too). Only meaningful when viewing
    # today: a future/past day chip has no "next" relative to right now.
    upcoming_activity = None
    if is_today_view:
        candidates = sorted(
            (a for a in activities
             if a.day == day and a.person in view_people and a.start_time and a.start_time >= now.time()),
            key=lambda a: a.start_time,
        )
        if candidates:
            act = candidates[0]
            upcoming_activity = {
                'person_name': _person_label(act.person, settings),
                'label': act.label,
                'time_range': act.time_range_label(),
                'accompanied_by_name': _person_label(act.accompanied_by, settings) if act.accompanied_by else '',
                'picked_up_by_name': _person_label(act.picked_up_by, settings) if act.picked_up_by else '',
                'location': act.location,
                'items_to_bring': act.items_to_bring,
            }

    orders = {}
    for o in TaskOrder.objects.filter(family=family):
        orders.setdefault(o.person, {})[o.task_id] = o.order
    levels = {s.person: _level_for(s.total, settings.star_milestone) for s in KidStars.objects.filter(family=family)}

    # "À préparer pour demain" — any task whose id contains 'demain' (currently sac_demain,
    # lunchbox_demain in task_logic.py; found by substring rather than hardcoded so a future
    # '..._demain' task picks itself up automatically) for whoever's visible. Read-only summary
    # here — the real checkbox stays in the person's own phase card below.
    tomorrow_prep = []

    all_people = _family_people(settings)
    cards = []
    for person in view_people:
        day_mode = active_day_mode(family, person, real_date)
        task_list = _apply_task_overrides(
            family, person, day, real_date, settings, activities, custom_tasks,
            holiday_today, holiday_tomorrow, day_mode=day_mode,
        )
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
        if is_today_view:
            for x in task_list:
                if 'demain' in x['id']:
                    tomorrow_prep.append({
                        'name': _person_label(person, settings), 'label': x['label'], 'done': x['done'],
                    })
        phases = [(pk, pl, apply_order(ts, orders.get(person, {}))) for pk, pl, ts in group_by_phase(task_list)]
        phase_cards = []
        for phase_key, phase_label, tasks in phases:
            checkable = [x for x in tasks if not x['info'] and not x['not_applicable']]
            done_count = sum(1 for x in checkable if x['done'])
            phase_cards.append({
                'phase_key': phase_key,
                'phase_label': phase_label,
                'tasks': tasks,
                'pct': round(done_count / len(checkable) * 100) if checkable else 0,
                'remaining': len(checkable) - done_count,
                'open_default': (not is_today_view) or (phase_key == current_phase),
            })
        cards.append({
            'person': person,
            'name': _person_label(person, settings),
            'phase_cards': phase_cards,
            'checkable_by_viewer': is_parent or person == membership.kid_person,
            'level': levels.get(person, 1) if person in ('fille', 'fils') else None,
            'day_mode': day_mode,
            'other_people': [(p, _person_label(p, settings)) for p in all_people if p != person],
        })

    day_chips = [{'key': d, 'label': DAY_FULL[d], 'full': DAY_FULL[d],
                  'is_today': i == today_idx, 'is_selected': d == day}
                 for i, d in enumerate(DAYS)]

    kid_cards = [c for c in cards if c['person'] in ('fille', 'fils')]
    parent_cards = [c for c in cards if c['person'] in ('maman', 'papa')] if is_parent else []

    return render(request, 'planner/today.html', {
        'kid_cards': kid_cards, 'parent_cards': parent_cards, 'day': day, 'day_chips': day_chips,
        'real_date': real_date, 'settings': settings, 'is_parent': is_parent,
        'kid_unassigned': not is_parent and not membership.kid_person,
        'who_options': who_options, 'who': who,
        'upcoming_activity': upcoming_activity, 'tomorrow_prep': tomorrow_prep,
        'tonight_recipe': tonight_recipe, 'day_mode_choices': DAY_MODE_CHOICES,
    })


def _exception_id_sets(family, person, date):
    """Splits a person's active TaskException rows for a given date into (disabled_ids,
    not_applicable_ids) — see task_logic.split_by_exceptions for how these are applied.
    'reassigned' rows are handled separately by _reassignment_maps."""
    disabled_ids, not_applicable_ids = set(), set()
    for exc in TaskException.objects.filter(family=family, person=person, active=True):
        if exc.kind == 'disabled_once' and exc.date == date:
            disabled_ids.add(exc.task_id)
        elif exc.kind == 'disabled_from' and exc.date <= date:
            disabled_ids.add(exc.task_id)
        elif exc.kind == 'not_applicable' and exc.date == date:
            not_applicable_ids.add(exc.task_id)
    return disabled_ids, not_applicable_ids


def _reassignment_maps(family, date):
    """Splits active TaskException(kind='reassigned') rows for one date into:
    - outgoing: person -> {task_id} handed away (dropped from that person's own list)
    - incoming: person -> [(from_person, task_id)] handed to them for that date
    See _apply_task_overrides for how both are folded into the final per-person task list;
    this is what makes 'passer une tâche à l'autre parent' (point 5) work."""
    outgoing, incoming = {}, {}
    for exc in TaskException.objects.filter(family=family, kind='reassigned', active=True, date=date):
        if not exc.reassigned_to:
            continue
        outgoing.setdefault(exc.person, set()).add(exc.task_id)
        incoming.setdefault(exc.reassigned_to, []).append((exc.person, exc.task_id))
    return outgoing, incoming


def _apply_task_overrides(family, person, day, real_date, settings, activities, custom_tasks,
                           holiday_today, holiday_tomorrow, day_mode='normal', own_task_list=None):
    """Builds a person's final task list for one date: their generated/custom tasks, with
    TaskException disabled/not_applicable overrides applied and reassignments folded in
    (tasks handed off to this person appear here with a 'reassigned_from_label'; tasks
    handed away disappear). Shared by views.today (display) and _checkable_ids_for
    (completion/star calculation) so the two stay in lockstep."""
    if own_task_list is None:
        own_task_list = tasks_for(person, day, settings, activities, holiday_today, holiday_tomorrow,
                                   custom_tasks, day_mode=day_mode)
    disabled_ids, not_applicable_ids = _exception_id_sets(family, person, real_date)
    outgoing, incoming = _reassignment_maps(family, real_date)
    disabled_ids = disabled_ids | outgoing.get(person, set())
    task_list = split_by_exceptions(own_task_list, disabled_ids, not_applicable_ids)
    for from_person, task_id in incoming.get(person, []):
        from_mode = active_day_mode(family, from_person, real_date)
        from_list = tasks_for(from_person, day, settings, activities, holiday_today, holiday_tomorrow,
                               custom_tasks, day_mode=from_mode)
        source = next((x for x in from_list if x['id'] == task_id), None)
        if source:
            task_list.append(dict(
                source, id=f'reassigned_{from_person}_{task_id}', not_applicable=False,
                reassigned_from_label=_person_label(from_person, settings),
            ))
    return task_list


def _checkable_ids_for(person, day, family):
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))
    real_date = _real_date_for_day(day)
    holiday_today = is_zone_b_holiday(real_date)
    holiday_tomorrow = is_zone_b_holiday(real_date + datetime.timedelta(days=1))
    day_mode = active_day_mode(family, person, real_date)
    task_list = _apply_task_overrides(
        family, person, day, real_date, settings, activities, custom_tasks,
        holiday_today, holiday_tomorrow, day_mode=day_mode,
    )
    return {x['id'] for x in task_list if not x['info'] and not x['not_applicable']}


def _award_star_if_day_complete(family, person, day, real_date):
    """Called after a kid's task is checked off. If that completes every checkable task for
    the day, silently banks one star (see StarAward/KidStars) and reports whether this star
    just crossed that family's star_milestone threshold (FamilySettings.star_milestone) — the
    one moment the kid actually sees anything. Also returns the parent-defined reward text
    (FamilySettings.star_reward_text) when a milestone was just reached, so the caller can pass
    it through to the celebration popup instead of a hardcoded message."""
    checkable_ids = _checkable_ids_for(person, day, family)
    if not checkable_ids:
        return False, None, None
    done_ids = set(TaskCompletion.objects.filter(
        family=family, person=person, date=real_date, done=True
    ).values_list('task_id', flat=True))
    if not checkable_ids.issubset(done_ids):
        return False, None, None
    _, created = StarAward.objects.get_or_create(family=family, person=person, date=real_date)
    if not created:
        return False, None, None
    settings = FamilySettings.load(family)
    milestone = max(1, settings.star_milestone)
    stars, _ = KidStars.objects.get_or_create(family=family, person=person)
    stars.total += 1
    reached = stars.total // milestone
    milestone_reached = reached > stars.milestones_shown
    reward_text = None
    if milestone_reached:
        stars.milestones_shown = reached
        reward_text = settings.star_reward_text or None
    stars.save()
    return milestone_reached, stars.total, reward_text


@login_required
@require_POST
def toggle_task(request):
    family = _get_family(request)
    person = request.POST['person']
    if not _can_act_on(request, person):
        raise PermissionDenied
    task_id = request.POST['task_id']
    day = request.POST['day']
    done = request.POST['done'] == '1'
    real_date = _real_date_for_day(day)
    TaskCompletion.objects.update_or_create(
        family=family, person=person, date=real_date, task_id=task_id, defaults={'done': done}
    )
    milestone_reached, stars_total, reward_text = False, None, None
    if done and person in ('fille', 'fils'):
        milestone_reached, stars_total, reward_text = _award_star_if_day_complete(family, person, day, real_date)
    return JsonResponse({
        'ok': True, 'milestone_reached': milestone_reached, 'stars_total': stars_total,
        'reward_text': reward_text,
    })


@login_required
@require_POST
def timer_task(request):
    family = _get_family(request)
    person = request.POST['person']
    if not _can_act_on(request, person):
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
    if not _can_act_on(request, person):
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
    milestone = max(1, settings.star_milestone)
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
            'level': _level_for(stars.total, milestone),
            'in_cycle': stars.total % milestone,
            'milestone': milestone,
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


def _week_start_from_request(request):
    """Resolves the Monday to display from ?week=YYYY-MM-DD (either param may be absent or
    invalid, in which case today's week is used — this keeps week_view's default behavior
    unchanged when no navigation has happened yet)."""
    today_monday = _monday_of(datetime.date.today())
    week_param = request.GET.get('week')
    if week_param:
        try:
            return _monday_of(datetime.date.fromisoformat(week_param))
        except ValueError:
            pass
    return today_monday


@login_required
def week_view(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    people = _family_people(settings)

    week_start = _week_start_from_request(request)
    today_monday = _monday_of(datetime.date.today())
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
            day_mode = active_day_mode(family, p, real_date)
            for x in tasks_for(p, d, settings, activities, holiday, False, day_mode=day_mode):
                if x['id'] in MENAGE_DAILY_IDS or pillar_for(x['id'], x['period']) != 'menage':
                    continue
                short = DEEP_CLEAN_ROOMS[d] if x['id'] == 'deepclean' else MENAGE_SHORT_LABELS.get(x['id'], x['label'])
                by_label.setdefault(short, []).append(_person_label(p, settings))
        menage_cells.append([f"{' & '.join(names)} : {label}" for label, names in by_label.items()])

        # An Activity with `specific_date` set is a one-off occurrence, shown only on the
        # exact date it falls on (never recurring); one without it shows every week on its
        # regular `day` — see Activity.specific_date and task_logic.find_schedule_conflicts.
        day_activities = [
            a for a in activities if a.person in people and (
                (a.specific_date and a.specific_date == real_date) or
                (not a.specific_date and a.day == d)
            )
        ]
        conflicting_ids = find_schedule_conflicts(day_activities)
        activites_cells.append([
            {
                'text': (
                    f"{_person_label(a.person, settings)} : {a.label}"
                    + (f" ({a.time_range_label()})" if a.time_range_label() else '')
                    + (' · ponctuel' if a.specific_date else '')
                ),
                'overlap': a.id in conflicting_ids,
            }
            for a in day_activities
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
        'week_start': week_start, 'week_end': week_start + datetime.timedelta(days=6),
        'prev_week': week_start - datetime.timedelta(days=7),
        'next_week': week_start + datetime.timedelta(days=7),
        'is_current_week': week_start == today_monday, 'current_week': today_monday,
        'week_note': settings.week_note,
    })


@login_required
@require_POST
def duplicate_week(request):
    """Copies the displayed week's WeeklyMenuEntry rows (the day → recipe menu plan) onto
    the following week. Scope, decided here: only WeeklyMenuEntry is duplicated — recurring
    Activity rows (plain `day`, no `specific_date`) already repeat every week on their own,
    and one-off `specific_date` Activity rows are deliberately NOT carried over, since
    "duplicate" for a dated one-off event is ambiguous (should the date shift by 7 days? is
    it still relevant?) and better left to an explicit per-activity action later. A day
    that already has a menu entry in the target week is left untouched, so duplicating never
    silently overwrites a menu someone already planned."""
    family = _get_family(request)
    week_param = request.POST.get('week')
    try:
        source_week = _monday_of(datetime.date.fromisoformat(week_param))
    except (TypeError, ValueError):
        source_week = _monday_of(datetime.date.today())
    target_week = source_week + datetime.timedelta(days=7)

    already_planned_days = set(WeeklyMenuEntry.objects.filter(
        family=family, week_start=target_week
    ).values_list('day', flat=True))

    copied = 0
    for entry in WeeklyMenuEntry.objects.filter(family=family, week_start=source_week):
        if entry.day in already_planned_days or not entry.recipe_id:
            continue
        WeeklyMenuEntry.objects.create(
            family=family, week_start=target_week, day=entry.day, recipe_id=entry.recipe_id
        )
        copied += 1

    if copied:
        messages.success(request, f"Menu dupliqué vers la semaine suivante ({copied} jour(s) copié(s)).")
    else:
        messages.info(request, "Rien à dupliquer : la semaine suivante a déjà un menu pour ces jours, "
                                "ou la semaine affichée n'a pas de menu.")
    return redirect(f"{reverse('week')}?week={target_week.isoformat()}")


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
        'settings': settings, 'grouped': grouped, 'menu_category': MENU_GROCERY_CATEGORY,
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
@require_POST
def toggle_grocery_home(request):
    """Toggles 'already_home' (Lot 4, point 2) — marks an item as already owned so it's
    visually excluded from the "à acheter" view without removing it from the list."""
    family = _get_family(request)
    item = GroceryItem.objects.get(pk=request.POST['item_id'], family=family)
    item.already_home = request.POST['already_home'] == '1'
    item.save(update_fields=['already_home'])
    return JsonResponse({'ok': True})


@login_required
@require_POST
def edit_grocery(request):
    """Renames/recategorizes/requantifies an existing grocery item (Lot 4, point 4)."""
    family = _get_family(request)
    item = GroceryItem.objects.get(pk=request.POST.get('item_id'), family=family)
    name = request.POST.get('name', '').strip()
    if not name:
        messages.error(request, "Le nom de l'article ne peut pas être vide.")
        return redirect('maison')
    item.name = name
    item.category = request.POST.get('category', '').strip()
    qty_raw = request.POST.get('quantity', '').strip()
    if qty_raw:
        try:
            item.quantity = Decimal(qty_raw.replace(',', '.'))
        except InvalidOperation:
            messages.error(request, "Quantité invalide.")
            return redirect('maison')
    else:
        item.quantity = None
    item.unit = request.POST.get('unit', '').strip()
    item.save()
    messages.success(request, "Article modifié.")
    return redirect('maison')


@login_required
@require_POST
def delete_grocery(request):
    """Deletes a grocery item outright (Lot 4, point 4) — separate from toggle_grocery,
    which only checks/unchecks it."""
    family = _get_family(request)
    GroceryItem.objects.filter(pk=request.POST.get('item_id'), family=family).delete()
    messages.success(request, "Article supprimé.")
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
    # Unfiltered — used for the per-day recipe dropdown, which must always offer every
    # recipe regardless of the "favoris" display filter below.
    recipes = Recipe.objects.filter(family=family).order_by('-is_favorite', 'category', 'name')
    favoris_only = request.GET.get('favoris') == '1'
    display_recipes = recipes.filter(is_favorite=True) if favoris_only else recipes
    by_cat = {}
    for r in display_recipes:
        by_cat.setdefault(r.category, []).append(r)

    entries = {e.day: e.recipe_id for e in WeeklyMenuEntry.objects.filter(family=family, week_start=week_start)}
    chosen_ids = [v for v in entries.values() if v]
    chosen_recipes = Recipe.objects.filter(family=family, id__in=chosen_ids)
    all_ingredients = Recipe.aggregate_ingredients(chosen_recipes)
    for ing in all_ingredients:
        qty_text = format_quantity(ing['quantity'])
        ing['quantity_display'] = f"{qty_text} {ing['unit']}".strip() if qty_text else ing['unit']

    day_rows = [{'day': d, 'label': DAY_FULL[d], 'selected': entries.get(d)} for d in DAYS]
    recipe_form = RecipeForm()
    pending_conflicts = None

    if request.method == 'POST':
        if 'add_recipe' in request.POST:
            form = RecipeForm(request.POST, request.FILES)
            if form.is_valid():
                recipe = form.save(commit=False)
                recipe.family = family
                recipe.save()
                messages.success(request, "Recette enregistrée.")
                return redirect('menu')
            recipe_form = form
        elif 'delete_recipe' in request.POST:
            Recipe.objects.filter(id=request.POST['delete_recipe'], family=family).delete()
            messages.success(request, "Recette supprimée.")
            return redirect('menu')
        elif 'set_day' in request.POST:
            day = request.POST['set_day']
            recipe_id = request.POST.get('recipe_id') or None
            if recipe_id and not Recipe.objects.filter(id=recipe_id, family=family).exists():
                messages.error(request, "Recette invalide.")
                return redirect('menu')
            WeeklyMenuEntry.objects.update_or_create(
                family=family, week_start=week_start, day=day, defaults={'recipe_id': recipe_id}
            )
            return redirect('menu')
        elif 'copy_to_courses' in request.POST:
            # Explicit conflict handling (Lot 4, point 5): copy_to_courses used to be a
            # silent get_or_create — an item already checked "acheté" that becomes needed
            # again (new week, ingredient required again) stayed silently checked, even
            # though it should be bought again. Now: if any needed ingredient already
            # exists as a *checked* GroceryItem, we stop and ask explicitly (pending_conflicts
            # below) instead of guessing either way. `resolve_returning` ('uncheck' puts them
            # back to "à acheter", 'keep' leaves them checked as-is) is how the confirmation
            # screen answers that question; recipes/quantities are recomputed from the current
            # weekly menu rather than trusted from the first request.
            resolution = request.POST.get('resolve_returning')
            names = [ing['name'] for ing in all_ingredients]
            existing_by_name = {i.name: i for i in GroceryItem.objects.filter(family=family, name__in=names)}
            returning_checked = [
                existing_by_name[n] for n in names
                if existing_by_name.get(n) is not None and existing_by_name[n].checked
            ]
            if returning_checked and resolution not in ('uncheck', 'keep'):
                pending_conflicts = returning_checked
            else:
                for ing in all_ingredients:
                    item = existing_by_name.get(ing['name'])
                    if item is None:
                        GroceryItem.objects.create(
                            family=family, name=ing['name'], category=MENU_GROCERY_CATEGORY,
                            quantity=ing['quantity'], unit=ing['unit'],
                        )
                    else:
                        item.quantity = ing['quantity']
                        item.unit = ing['unit']
                        if resolution == 'uncheck' and item.checked:
                            item.checked = False
                        item.save(update_fields=['quantity', 'unit', 'checked'])
                messages.success(request, "Ingrédients ajoutés à la liste de courses.")
                return redirect('menu')

    return render(request, 'planner/menu.html', {
        'by_cat': by_cat, 'day_rows': day_rows, 'recipes': recipes,
        'all_ingredients': all_ingredients, 'recipe_form': recipe_form,
        'favoris_only': favoris_only, 'pending_conflicts': pending_conflicts,
    })


@login_required
@require_POST
def toggle_recipe_favorite(request):
    family = _get_family(request)
    recipe = Recipe.objects.get(pk=request.POST['recipe_id'], family=family)
    recipe.is_favorite = request.POST['is_favorite'] == '1'
    recipe.save(update_fields=['is_favorite'])
    return JsonResponse({'ok': True})


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
            days_selected = [d for d in request.POST.getlist('task_days') if d in DAYS]
            if label and days_selected:
                CustomTask.objects.create(
                    family=family,
                    person=request.POST.get('task_person', 'fille'),
                    days=days_selected,
                    period=request.POST.get('task_period', 'matin'),
                    label=label,
                )
                messages.success(request, "Tâche ajoutée.")
            else:
                messages.error(request, "Merci d'indiquer un intitulé et au moins un jour.")
        elif 'save_rewards' in request.POST:
            try:
                milestone = int(request.POST.get('star_milestone', settings.star_milestone))
            except (TypeError, ValueError):
                milestone = settings.star_milestone
            settings.star_milestone = max(1, milestone)
            settings.star_reward_text = request.POST.get('star_reward_text', '').strip()
            settings.save()
            messages.success(request, "Récompense enregistrée.")
        elif 'regenerate_tablet_token' in request.POST:
            settings.regenerate_tablet_token()
            messages.success(request, "Lien tablette régénéré.")
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
    tablet_url = request.build_absolute_uri(reverse('tablet', args=[settings.tablet_token])) if settings.tablet_token else ''
    task_exceptions = list(TaskException.objects.filter(family=family, active=True).order_by('-date'))
    for exc in task_exceptions:
        exc.person_name = _person_label(exc.person, settings)
        exc.reassigned_to_name = _person_label(exc.reassigned_to, settings) if exc.reassigned_to else ''
    return render(request, 'planner/settings.html', {
        'settings': settings, 'activities': activities, 'custom_tasks': custom_tasks,
        'days': DAYS, 'day_full': DAY_FULL, 'members': members, 'tablet_url': tablet_url,
        'task_exceptions': task_exceptions,
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
def set_member_kid(request, pk):
    """A parent assigns (or clears) which kid a shared 'enfants' account represents — see
    FamilyMembership.kid_person. This is the only way that field gets set; there's no
    self-service option since a kid account shouldn't be able to grant itself another kid's
    tasks. Only affects members with role='enfants' — a no-op (silently ignored, same as
    promote_member on a bad pk) on anyone else."""
    family = _get_family(request)
    settings = FamilySettings.load(family)
    kid_person = request.POST.get('kid_person', '')
    membership = FamilyMembership.objects.filter(pk=pk, family=family, role='enfants').first()
    if not membership:
        messages.error(request, "Action impossible.")
    elif kid_person and kid_person not in _kids_people(settings):
        messages.error(request, "Enfant invalide.")
    else:
        membership.kid_person = kid_person
        membership.save(update_fields=['kid_person'])
        messages.success(request, "Compte associé à un enfant." if kid_person else "Association retirée.")
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


@ratelimit(key='ip', rate='60/m', method='GET', block=True)
def tablet_view(request, token):
    """Read-only, no-login kitchen-tablet display (see FamilySettings.tablet_token). Anyone
    with the long, unguessable token in the URL can view — but only view: today's cards render
    with checkable_by_viewer=False (disabled checkboxes, no JS wired up here) so nothing on
    this page can be edited. Day navigation is a plain `?day=` link, same token-bearing URL."""
    settings = FamilySettings.objects.select_related('family').filter(tablet_token=token).first() if token else None
    if not settings:
        raise Http404("Lien tablette invalide.")
    family = settings.family
    _ensure_seed_data(family)
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))

    day = request.GET.get('day')
    today_idx = datetime.date.today().weekday()
    if day not in DAYS:
        day = DAYS[today_idx]
    real_date = datetime.date.today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))
    holiday_today = is_zone_b_holiday(real_date)
    holiday_tomorrow = is_zone_b_holiday(real_date + datetime.timedelta(days=1))

    people = _family_people(settings)
    orders = {}
    for o in TaskOrder.objects.filter(family=family):
        orders.setdefault(o.person, {})[o.task_id] = o.order

    cards = []
    for person in people:
        day_mode = active_day_mode(family, person, real_date)
        task_list = _apply_task_overrides(
            family, person, day, real_date, settings, activities, custom_tasks,
            holiday_today, holiday_tomorrow, day_mode=day_mode,
        )
        completions = {
            tc.task_id: tc
            for tc in TaskCompletion.objects.filter(family=family, person=person, date=real_date)
        }
        for x in task_list:
            tc = completions.get(x['id'])
            x['done'] = tc.done if tc else False
            x['seconds_spent'] = tc.seconds_spent if tc else 0
            x['timer_running'] = False
            x['timer_started_ms'] = None
        phases = [(pk, pl, apply_order(ts, orders.get(person, {}))) for pk, pl, ts in group_by_phase(task_list)]
        phase_cards = []
        for phase_key, phase_label, tasks in phases:
            checkable = [x for x in tasks if not x['info'] and not x['not_applicable']]
            done_count = sum(1 for x in checkable if x['done'])
            phase_cards.append({
                'phase_key': phase_key,
                'phase_label': phase_label,
                'tasks': tasks,
                'pct': round(done_count / len(checkable) * 100) if checkable else 0,
                'remaining': len(checkable) - done_count,
            })
        cards.append({
            'person': person,
            'name': _person_label(person, settings),
            'phase_cards': phase_cards,
            'checkable_by_viewer': False,
            'level': None,
        })

    day_chips = [{'key': d, 'label': DAY_FULL[d], 'full': DAY_FULL[d],
                  'is_today': i == today_idx, 'is_selected': d == day}
                 for i, d in enumerate(DAYS)]

    week_start = _monday_of(datetime.date.today())
    menu_entry = WeeklyMenuEntry.objects.filter(
        family=family, week_start=week_start, day=day
    ).select_related('recipe').first()
    dinner = menu_entry.recipe if menu_entry else None

    day_idx = DAYS.index(day)
    upcoming = sorted(
        activities, key=lambda a: ((DAYS.index(a.day) - day_idx) % 7, a.start_time or datetime.time.max)
    )[:8]
    for a in upcoming:
        a.person_name = _person_label(a.person, settings)

    return render(request, 'planner/tablet.html', {
        'kid_cards': [c for c in cards if c['person'] in ('fille', 'fils')],
        'parent_cards': [c for c in cards if c['person'] in ('maman', 'papa')],
        'day': day, 'day_chips': day_chips, 'real_date': real_date, 'settings': settings,
        'dinner': dinner, 'upcoming': upcoming,
    })


@login_required
@parent_required
def edit_custom_task(request, pk):
    """Classic edit for a CustomTask (label/person/days/period) — the "modifier" half of
    point 2. Only CustomTask rows are editable this way: a generated task_logic.py task
    (e.g. 'lit', 'priere_m') has no row to edit, since it's produced by a Python function,
    not stored data — for those, see create_task_exception (disable) and reassign_task
    (hand off to someone else) instead."""
    family = _get_family(request)
    task = CustomTask.objects.filter(pk=pk, family=family).first()
    if not task:
        messages.error(request, "Tâche introuvable.")
        return redirect('settings')
    if request.method == 'POST':
        label = request.POST.get('task_label', '').strip()
        days_selected = [d for d in request.POST.getlist('task_days') if d in DAYS]
        if label and days_selected:
            task.label = label
            task.person = request.POST.get('task_person', task.person)
            task.period = request.POST.get('task_period', task.period)
            task.days = days_selected
            task.save()
            messages.success(request, "Tâche modifiée.")
        else:
            messages.error(request, "Merci d'indiquer un intitulé et au moins un jour.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def create_task_exception(request):
    """Creates a disabled_once / disabled_from / not_applicable TaskException — point 1's
    "désactiver une tâche". Posted from the per-task controls revealed by the "Gérer les
    tâches" toggle on 'Aujourd'hui' (today.html [data-exception-mode]), the same on-demand
    reveal pattern as the reorder-mode arrows in design-v2."""
    family = _get_family(request)
    person = request.POST.get('person')
    task_id = request.POST.get('task_id', '').strip()
    day = request.POST.get('day')
    kind = request.POST.get('kind')
    valid_kinds = {k for k, _ in TASK_EXCEPTION_KIND_CHOICES if k != 'reassigned'}
    if person not in PERSON_KEYS or not task_id or day not in DAYS or kind not in valid_kinds:
        messages.error(request, "Action impossible.")
        return redirect(f"{reverse('today')}?day={day}" if day in DAYS else reverse('today'))
    real_date = _real_date_for_day(day)
    TaskException.objects.create(family=family, person=person, task_id=task_id, kind=kind, date=real_date)
    messages.success(request, "Tâche mise à jour pour ce jour.")
    return redirect(f"{reverse('today')}?day={day}")


@login_required
@parent_required
@require_POST
def reassign_task(request):
    """Hands a task off to another family member for one day (point 5) by creating a
    TaskException(kind='reassigned'): it disappears from `person`'s list and appears in
    `reassigned_to`'s (see views._reassignment_maps / _apply_task_overrides). Chosen over a
    dedicated reassignment table since TaskException already models "an override on a task
    for a date" — adding a `reassigned_to` field reuses that shape instead of introducing a
    parallel mechanism."""
    family = _get_family(request)
    person = request.POST.get('person')
    task_id = request.POST.get('task_id', '').strip()
    day = request.POST.get('day')
    reassigned_to = request.POST.get('reassigned_to')
    if (person not in PERSON_KEYS or reassigned_to not in PERSON_KEYS or person == reassigned_to
            or not task_id or day not in DAYS):
        messages.error(request, "Réattribution impossible.")
        return redirect(f"{reverse('today')}?day={day}" if day in DAYS else reverse('today'))
    real_date = _real_date_for_day(day)
    TaskException.objects.create(
        family=family, person=person, task_id=task_id, kind='reassigned', date=real_date,
        reassigned_to=reassigned_to,
    )
    messages.success(request, "Tâche réattribuée pour ce jour.")
    return redirect(f"{reverse('today')}?day={day}")


@login_required
@parent_required
@require_POST
def reactivate_task_exception(request, pk):
    """'Reactivating' means turning the underlying task back ON — see TaskException.active's
    docstring: flipping active to False cancels the override (disable/not_applicable/
    reassignment alike), which is the one mechanism point 1 asks for to review/undo any
    exception from the settings page."""
    family = _get_family(request)
    exc = TaskException.objects.filter(pk=pk, family=family).first()
    if exc:
        exc.active = False
        exc.save(update_fields=['active'])
        messages.success(request, "Tâche réactivée.")
    else:
        messages.error(request, "Action impossible.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def set_day_mode(request):
    """Sets (or, choosing 'normal', clears) a person's DayMode for one calendar day — point
    4's UI for task_logic.active_day_mode. Surfaced on 'Aujourd'hui' (per person-card select)
    rather than in settings, since "is X away/on a lightened day today" is a decision
    naturally tied to the specific day being viewed, not a standing family setting."""
    family = _get_family(request)
    person = request.POST.get('person')
    day = request.POST.get('day')
    mode = request.POST.get('mode')
    valid_modes = {m for m, _ in DAY_MODE_CHOICES}
    if person not in PERSON_KEYS or day not in DAYS or mode not in valid_modes:
        messages.error(request, "Action impossible.")
        return redirect('today')
    real_date = _real_date_for_day(day)
    if mode == 'normal':
        DayMode.objects.filter(family=family, person=person, date=real_date).delete()
    else:
        DayMode.objects.update_or_create(
            family=family, person=person, date=real_date, defaults={'mode': mode},
        )
    return redirect(f"{reverse('today')}?day={day}")
