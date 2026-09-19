import datetime
import functools
import json
from urllib.parse import urlencode
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
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
    HelpRequest, RoutineReminder, REMINDER_PHASE_CHOICES, Checklist,
    CUSTOM_TASK_FREQUENCIES, MONTHLY_NTH_CHOICES, PHASE_CHOICES,
)
from .task_logic import (
    DAYS, DAY_FULL, SCHOOL_DAYS, WEEKEND_DAYS, tasks_for, next_day, pillar_for,
    is_zone_b_holiday, DEEP_CLEAN_ROOMS, group_by_phase, apply_order, parse_free_time,
    split_by_exceptions, find_schedule_conflicts, active_day_mode, activities_on,
    days_summary, custom_task_occurs_on,
    phase_for_time,
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
    `person`. A parent may act on anyone. The 'enfants' account is shared by every kid in the
    family — they're on the same screen doing tasks together — so it may act on any kid."""
    membership = request.user.familymembership
    if membership.role in PARENT_ROLES:
        return True
    return person in ('fille', 'fils')


def _today():
    """La date « aujourd'hui » de la famille, en heure locale Django (Europe/Paris).

    À ne jamais remplacer par datetime.date.today(), qui suit l'horloge système du serveur :
    en production celle-ci est en UTC, donc entre minuit et 2 h du matin à Paris elle renvoie
    encore la veille — la journée de l'enfant changerait avec une ou deux heures de retard."""
    return timezone.localdate()


def _now():
    """L'heure locale de la famille (Europe/Paris), naïve, pour comparer aux heures saisies
    dans l'app (début d'activité, plage horaire) qui sont elles aussi locales et naïves."""
    return timezone.localtime().replace(tzinfo=None)


def _monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def _real_date_for_day(day):
    today_idx = _today().weekday()
    return _today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))


def _current_phase_now(now_time):
    """Which accordion phase opens by default on 'Aujourd'hui'. Delegates to
    task_logic.phase_for_time so the same cut-offs decide where a timed activity is slotted
    — one rule, not two that can drift. Deliberately time-of-day only: it has no relation to
    any one family member's school/work hours (see task_logic.is_bureau_day etc.)."""
    return phase_for_time(now_time)


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


# La formulation vit avec la règle, dans task_logic : les modèles la réutilisent aussi.
_days_summary = days_summary


def _read_frequency(request, prefix='task'):
    """Lit les champs de fréquence d'un formulaire de tâche personnalisée.

    Renvoie (defaults, erreur). Chaque fréquence a besoin d'une information de plus, et on
    refuse plutôt que de deviner : une tâche « une semaine sur deux » sans semaine de
    référence ou « une seule fois » sans date ne veut rien dire, et se rabattre en silence
    sur l'hebdomadaire ferait apparaître la tâche bien plus souvent que demandé."""
    frequency = request.POST.get(f'{prefix}_frequency', 'weekly')
    if frequency not in dict(CUSTOM_TASK_FREQUENCIES):
        frequency = 'weekly'
    defaults = {
        'frequency': frequency, 'anchor_week': None,
        'monthly_nth': None, 'specific_date': None,
    }

    if frequency == 'once':
        raw = request.POST.get(f'{prefix}_date', '').strip()
        try:
            defaults['specific_date'] = datetime.date.fromisoformat(raw)
        except ValueError:
            return None, "Choisissez une date pour une tâche ponctuelle."

    elif frequency == 'biweekly':
        raw = request.POST.get(f'{prefix}_anchor', '').strip()
        if raw:
            try:
                anchor = datetime.date.fromisoformat(raw)
            except ValueError:
                return None, "Semaine de référence invalide."
        else:
            anchor = _today()
        defaults['anchor_week'] = _monday_of(anchor)

    elif frequency == 'monthly':
        try:
            nth = int(request.POST.get(f'{prefix}_nth', 1))
        except (TypeError, ValueError):
            nth = 1
        if nth not in dict(MONTHLY_NTH_CHOICES):
            nth = 1
        defaults['monthly_nth'] = nth

    return defaults, None


def _custom_task_error(label, days):
    """One explicit message per missing field, rather than a single catch-all — the day
    picker can now be left empty by 'Personnaliser', so saying which half is missing matters."""
    if not label and not days:
        return "Merci d'indiquer un intitulé et de choisir au moins un jour."
    if not label:
        return "Merci d'indiquer un intitulé pour la tâche."
    return "Merci de choisir au moins un jour pour cette tâche."


def _wizard_banner(request, step, label, description, next_url=None, is_last=False):
    """Builds the progress-banner context for one screen of the 'Préparer notre semaine'
    wizard (Lot 5b), active only when the URL carries ?wizard=1 (see wizard_start below).
    Deliberately stateless: the query string is the only source of truth, so a screen visited
    without the param just renders normally, with no banner and nothing to clean up — see
    the module-level note near wizard_start for the full design rationale."""
    if request.GET.get('wizard') != '1':
        return None
    return {
        'step': step, 'total': 4, 'label': label, 'description': description,
        'next_url': next_url, 'is_last': is_last,
    }


def _week_preparation(family, settings, week_start):
    """État réel de préparation d'une semaine, calculé à partir des données.

    Le parcours guidé ne décide pas qu'une semaine est prête parce qu'on a cliqué quatre
    fois : chaque étape est relue dans la base pour la semaine concernée. Passer sur un
    écran sans rien y changer laisse donc l'étape « à faire », et une semaine préparée le
    mois dernier reste prête même si personne n'a rouvert le parcours.

    Trois états seulement : 'vide' (rien), 'partiel' (commencé, incomplet), 'ok'. Une étape
    qui n'a pas de notion de « complet » — les événements, la répartition — n'est jamais
    'partiel' pour rien : elle signale ce qui mérite un regard (un conflit d'horaires) et
    s'en tient là."""
    week_end = week_start + datetime.timedelta(days=6)
    activities = list(Activity.objects.filter(family=family))

    # 1. Événements : ceux qui tombent réellement dans la semaine, règle occurs_on partagée.
    week_dates = [week_start + datetime.timedelta(days=i) for i in range(7)]
    events = [a for d in week_dates for a in activities_on(activities, d)]
    conflicts = set()
    for d in week_dates:
        conflicts |= find_schedule_conflicts(activities_on(activities, d))

    if not events:
        events_state, events_detail = 'vide', "Aucun événement noté cette semaine."
    elif conflicts:
        events_state = 'partiel'
        events_detail = (f"{len(events)} événement{'s' if len(events) > 1 else ''}, "
                         f"dont {len(conflicts)} en conflit d'horaires.")
    else:
        events_state = 'ok'
        events_detail = f"{len(events)} événement{'s' if len(events) > 1 else ''} placé{'s' if len(events) > 1 else ''}."

    # 2. Répartition : exceptions et réattributions posées pour les jours de la semaine.
    overrides = TaskException.objects.filter(
        family=family, active=True, date__gte=week_start, date__lte=week_end
    ).count()
    custom = CustomTask.objects.filter(family=family).count()
    if overrides:
        tasks_state = 'ok'
        tasks_detail = f"{overrides} ajustement{'s' if overrides > 1 else ''} pour cette semaine."
    elif custom:
        tasks_state = 'ok'
        tasks_detail = (f"{custom} tâche{'s' if custom > 1 else ''} personnalisée"
                        f"{'s' if custom > 1 else ''} en place, aucun ajustement cette semaine.")
    else:
        tasks_state = 'vide'
        tasks_detail = "Routine de base, sans tâche personnalisée ni ajustement."

    # 3. Menus : un plat par jour, sur les sept jours de la semaine choisie.
    filled = set(
        WeeklyMenuEntry.objects.filter(family=family, week_start=week_start, recipe__isnull=False)
        .values_list('day', flat=True)
    )
    missing = [d for d in DAYS if d not in filled]
    if not filled:
        menu_state, menu_detail = 'vide', "Aucun repas choisi pour cette semaine."
    elif missing:
        menu_state = 'partiel'
        menu_detail = f"{len(filled)} jour{'s' if len(filled) > 1 else ''} sur 7 — manque {_days_summary(missing)}."
    else:
        menu_state, menu_detail = 'ok', "Les 7 repas du soir sont choisis."

    # 4. Courses : la liste issue de cette semaine précisément (les produits habituels,
    # sans semaine, ne comptent pas — sinon la liste paraîtrait toujours faite).
    week_items = list(GroceryItem.objects.filter(family=family, week_start=week_start))
    to_buy = [g for g in week_items if not g.checked and not g.already_home]
    if not week_items:
        groceries_state, groceries_detail = 'vide', "Liste pas encore générée depuis les menus."
    elif to_buy:
        groceries_state = 'partiel'
        groceries_detail = f"{len(to_buy)} produit{'s' if len(to_buy) > 1 else ''} encore à acheter."
    else:
        groceries_state = 'ok'
        groceries_detail = f"Les {len(week_items)} produits de la semaine sont cochés ou déjà à la maison."

    steps = [
        {'step': 1, 'label': 'Événements de la semaine', 'icon': 'icon-organisation',
         'state': events_state, 'detail': events_detail,
         'url': f"{reverse('week')}?wizard=1&step=1&week={week_start.isoformat()}"},
        {'step': 2, 'label': 'Répartition des tâches', 'icon': 'icon-exception',
         'state': tasks_state, 'detail': tasks_detail,
         'url': f"{reverse('settings')}?wizard=1&step=2"},
        {'step': 3, 'label': 'Menus', 'icon': 'icon-menu',
         'state': menu_state, 'detail': menu_detail,
         'url': f"{reverse('menu')}?wizard=1&step=3&week={week_start.isoformat()}"},
        {'step': 4, 'label': 'Courses', 'icon': 'icon-maison',
         'state': groceries_state, 'detail': groceries_detail,
         'url': f"{reverse('maison')}?wizard=1&step=4&week={week_start.isoformat()}"},
    ]
    return steps


WEEK_STATE_LABELS = {'ok': 'Prêt', 'partiel': 'À finir', 'vide': 'À faire'}


def _wizard_redirect(request, view_name, step=None):
    """Same as redirect(view_name), except it re-appends ?wizard=1&step=N when the request
    that triggered it was itself part of the wizard flow. Plain redirect()/reverse() drop the
    query string entirely, which would silently kick the user out of the wizard on the very
    first same-page action of a step (picking a recipe, adding a custom task...) — this is
    the only thing standing between "wizard=1 disappears" and "banner survives the step"."""
    url = reverse(view_name)
    if request.GET.get('wizard') == '1':
        use_step = step if step is not None else (request.GET.get('step') or '')
        if use_step:
            url = f"{url}?wizard=1&step={use_step}"
    return redirect(url)


@login_required
def today(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))

    day = request.GET.get('day')
    today_idx = _today().weekday()  # 0=lundi
    if day not in DAYS:
        day = DAYS[today_idx]

    real_date = _today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))
    holiday_today = is_zone_b_holiday(real_date)
    holiday_tomorrow = is_zone_b_holiday(real_date + datetime.timedelta(days=1))

    # Only today's date has a meaningful "current period" — on any other day chip every
    # phase card just opens expanded (see 'open_default' below).
    is_today_view = day == DAYS[today_idx]
    now = _now()
    current_phase = _current_phase_now(now.time())

    # Direct access to the evening meal — unlike "À venir"/"À préparer pour demain" this isn't
    # relative to right now, so it's shown for whichever day chip is selected, not just today.
    week_start = _monday_of(_today())
    menu_entry = WeeklyMenuEntry.objects.filter(
        family=family, week_start=week_start, day=day
    ).select_related('recipe').first()
    tonight_recipe = menu_entry.recipe if menu_entry else None

    membership = request.user.familymembership
    is_parent = membership.role in PARENT_ROLES
    # The 'enfants' account is shared by every kid in the family — they're together on one
    # screen doing tasks at the same time — so it always sees and can act on every kid's card.
    kids_people = _kids_people(settings)
    view_people = _family_people(settings) if is_parent else kids_people

    # "Toute la famille / Moi / chaque enfant" selector (?who=), parent-only: an 'enfants'
    # account already sees every kid via view_people above and has no single "me" to narrow
    # to, so the selector isn't offered to it (who_options stays None, see today.html). "Moi"
    # maps to the viewer's own person: their role for a parent (role is literally
    # 'maman'/'papa', the same string as the person key).
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
            (a for a in activities_on(activities, real_date)
             if a.person in view_people and a.start_time and a.start_time >= now.time()),
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
        day_mode, task_list, phases = _person_day_tasks(
            family, person, day, real_date, settings, activities, custom_tasks, orders,
            holiday_today, holiday_tomorrow,
        )
        if is_today_view:
            for x in task_list:
                if 'demain' in x['id']:
                    tomorrow_prep.append({
                        'name': _person_label(person, settings), 'label': x['label'], 'done': x['done'],
                    })
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
            'checkable_by_viewer': is_parent or person in ('fille', 'fils'),
            'level': levels.get(person, 1) if person in ('fille', 'fils') else None,
            'day_mode': day_mode,
            'other_people': [(p, _person_label(p, settings)) for p in all_people if p != person],
        })

    day_chips = [{'key': d, 'label': DAY_FULL[d], 'full': DAY_FULL[d],
                  'is_today': i == today_idx, 'is_selected': d == day}
                 for i, d in enumerate(DAYS)]

    kid_cards = [c for c in cards if c['person'] in ('fille', 'fils')]
    parent_cards = [c for c in cards if c['person'] in ('maman', 'papa')] if is_parent else []

    # La routine guidée ne porte que sur la journée en cours : sur un autre jour du bandeau,
    # on ne propose pas un parcours qui écrirait sur aujourd'hui.
    routine_remaining = sum(
        pc['remaining'] for c in kid_cards for pc in c['phase_cards']
    ) if is_today_view else 0

    # Bilan de préparation de la semaine en cours, affiché sur la carte d'entrée du parcours.
    # Calculé seulement pour un parent, qui est le seul à voir cette carte.
    prep_ready = 0
    prep_all_ready = False
    if is_parent:
        prep_steps = _week_preparation(family, settings, _monday_of(real_date))
        prep_ready = sum(1 for s in prep_steps if s['state'] == 'ok')
        prep_all_ready = prep_ready == len(prep_steps)

    return render(request, 'planner/today.html', {
        'kid_cards': kid_cards, 'parent_cards': parent_cards, 'day': day, 'day_chips': day_chips,
        'show_routine_entry': is_today_view and bool(kid_cards),
        'routine_remaining': routine_remaining,
        'prep_ready_count': prep_ready, 'prep_all_ready': prep_all_ready,
        'real_date': real_date, 'settings': settings, 'is_parent': is_parent,
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
                                   custom_tasks, day_mode=day_mode, date=real_date)
    disabled_ids, not_applicable_ids = _exception_id_sets(family, person, real_date)
    outgoing, incoming = _reassignment_maps(family, real_date)
    disabled_ids = disabled_ids | outgoing.get(person, set())
    task_list = split_by_exceptions(own_task_list, disabled_ids, not_applicable_ids)
    for from_person, task_id in incoming.get(person, []):
        from_mode = active_day_mode(family, from_person, real_date)
        from_list = tasks_for(from_person, day, settings, activities, holiday_today, holiday_tomorrow,
                               custom_tasks, day_mode=from_mode, date=real_date)
        source = next((x for x in from_list if x['id'] == task_id), None)
        if source:
            task_list.append(dict(
                source, id=f'reassigned_{from_person}_{task_id}', not_applicable=False,
                reassigned_from_label=_person_label(from_person, settings),
            ))
    return task_list


def _person_day_tasks(family, person, day, real_date, settings, activities, custom_tasks,
                      orders, holiday_today=None, holiday_tomorrow=None, day_mode=None):
    """Une personne, un jour : sa liste de tâches ordonnée, groupée par phase, avec l'état
    de complétion et de minuteur déjà attaché.

    Source unique partagée par la checklist ('Aujourd'hui'), la routine guidée et la
    tablette, pour qu'aucun de ces écrans ne puisse diverger sur ce qui reste à faire, dans
    quel ordre, ni sur ce que le mode du jour, les exceptions et les réattributions ont
    retiré. Retourne (day_mode, liste à plat, phases)."""
    if holiday_today is None:
        holiday_today = is_zone_b_holiday(real_date)
    if holiday_tomorrow is None:
        holiday_tomorrow = is_zone_b_holiday(real_date + datetime.timedelta(days=1))
    if day_mode is None:
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
        x['timer_started_ms'] = (
            int(tc.timer_started_at.timestamp() * 1000) if (tc and tc.timer_started_at) else None
        )
    phases = [(pk, pl, apply_order(ts, orders.get(person, {})))
              for pk, pl, ts in group_by_phase(task_list)]
    return day_mode, task_list, phases


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
    if done:
        # La tâche est faite : le « besoin d'aide » qui la concernait n'a plus lieu d'être.
        HelpRequest.objects.filter(
            family=family, person=person, date=real_date, task_id=task_id, active=True
        ).update(active=False)
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
def routine_view(request):
    """« Commencer nos routines » : un espace par enfant, côte à côte, sur l'écran partagé.

    Chaque enfant avance à son rythme — la tâche courante d'un espace est simplement la
    première tâche non faite de SA liste, dérivée à chaque affichage de TaskCompletion. Il
    n'y a donc aucun second système de progression : cocher ici ou depuis la checklist
    écrit exactement la même chose, les étoiles suivent les mêmes règles, et un
    rechargement repart de l'état réel.

    L'ordre personnalisé, le mode du jour, les exceptions et les réattributions sont ceux de
    _person_day_tasks, partagé avec « Aujourd'hui ». Les tâches non applicables et les lignes
    d'information sont exclues du parcours : on ne demande de faire que ce qui est à faire."""
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))
    orders = {}
    for o in TaskOrder.objects.filter(family=family):
        orders.setdefault(o.person, {})[o.task_id] = o.order

    real_date = _today()
    day = DAYS[real_date.weekday()]
    help_flags = set(
        HelpRequest.objects.filter(family=family, date=real_date, active=True)
        .values_list('person', 'task_id')
    )

    spaces = []
    for person in _kids_people(settings):
        day_mode, _task_list, phases = _person_day_tasks(
            family, person, day, real_date, settings, activities, custom_tasks, orders,
        )
        sequence = []
        for phase_key, phase_label, tasks in phases:
            for task in tasks:
                if task['info'] or task['not_applicable']:
                    continue
                sequence.append(dict(task, phase_label=phase_label))
        done_count = sum(1 for task in sequence if task['done'])
        current = next((task for task in sequence if not task['done']), None)
        spaces.append({
            'person': person,
            'name': _person_label(person, settings),
            'sequence': sequence,
            'current': current,
            'current_phase': current['phase_label'] if current else '',
            'needs_help': bool(current and (person, current['id']) in help_flags),
            'done_count': done_count,
            'total': len(sequence),
            'remaining': len(sequence) - done_count,
            'pct': round(done_count / len(sequence) * 100) if sequence else 0,
            'finished': bool(sequence) and done_count == len(sequence),
            'day_mode': day_mode,
            'can_act': _can_act_on(request, person),
        })

    return render(request, 'planner/routine.html', {
        'spaces': spaces, 'day': day, 'real_date': real_date,
    })


@login_required
@require_POST
def toggle_help(request):
    """Lève ou retire le « besoin d'aide » d'un enfant sur une tâche. Purement un drapeau
    d'affichage dans l'espace de cet enfant : il ne bloque pas l'autre, ne touche pas à la
    progression et n'envoie aucune notification externe."""
    family = _get_family(request)
    person = request.POST.get('person')
    if not _can_act_on(request, person):
        raise PermissionDenied
    task_id = request.POST.get('task_id', '').strip()
    if person not in PERSON_KEYS or not task_id:
        return JsonResponse({'ok': False}, status=400)
    wants_help = request.POST.get('help') == '1'
    real_date = _today()
    HelpRequest.objects.update_or_create(
        family=family, person=person, date=real_date, task_id=task_id,
        defaults={'active': wants_help},
    )
    return JsonResponse({'ok': True, 'needs_help': wants_help})


def _phase_progress(family, settings, people, day, real_date):
    """Reste-t-il quelque chose à faire, par (personne, phase) ? Dérivé de _person_day_tasks,
    donc exactement le même décompte que « Aujourd'hui » et que la routine guidée : un rappel
    ne peut pas prétendre qu'il reste des tâches là où l'écran affiche que tout est coché."""
    activities = list(Activity.objects.filter(family=family))
    custom_tasks = list(CustomTask.objects.filter(family=family))
    orders = {}
    for o in TaskOrder.objects.filter(family=family):
        orders.setdefault(o.person, {})[o.task_id] = o.order

    progress = {}
    for person in people:
        _day_mode, _flat, phases = _person_day_tasks(
            family, person, day, real_date, settings, activities, custom_tasks, orders,
        )
        for phase_key, phase_label, tasks in phases:
            checkable = [t for t in tasks if not t['info'] and not t['not_applicable']]
            done = sum(1 for t in checkable if t['done'])
            progress[(person, phase_key)] = {
                'phase_label': phase_label, 'total': len(checkable), 'done': done,
                'remaining': len(checkable) - done,
            }
    return progress


def _due_reminders(family, settings, now=None):
    """Les rappels à afficher maintenant, en heure locale Django (Europe/Paris).

    Un rappel s'affiche si — et seulement si — les rappels sont activés pour la famille,
    l'heure locale est hors de la plage de calme, le jour fait partie de ses jours, son
    heure est passée, il n'a pas été mis en sourdine aujourd'hui, et il reste réellement
    des tâches dans la phase visée. Autrement dit on ne rappelle jamais une routine déjà
    terminée, et jamais une phase vide.

    Volontairement calculé à la demande, sans tâche planifiée ni file d'attente : c'est ce
    qui permet de tenir la promesse « aucune infrastructure supplémentaire », au prix
    assumé de ne rien pouvoir afficher quand l'application est fermée."""
    if not settings.reminders_enabled:
        return []
    now = now or _now()
    if settings.in_quiet_hours(now.time()):
        return []

    today, current = now.date(), now.time()
    day = DAYS[today.weekday()]
    kids = set(_kids_people(settings))
    candidates = [
        r for r in RoutineReminder.objects.filter(family=family, active=True)
        if r.person in kids and r.occurs_on_day(day) and r.at_time <= current and r.acked_on != today
    ]
    if not candidates:
        return []

    progress = _phase_progress(family, settings, {r.person for r in candidates}, day, today)
    due = []
    for reminder in candidates:
        state = progress.get((reminder.person, reminder.phase))
        if not state or not state['remaining']:
            continue
        name = _person_label(reminder.person, settings)
        due.append({
            'id': reminder.id,
            'person': reminder.person,
            'person_name': name,
            'phase': reminder.phase,
            'phase_label': state['phase_label'],
            'label': reminder.label or reminder.default_label(name),
            'at_time': reminder.at_time.strftime('%H:%M'),
            'remaining': state['remaining'],
        })
    return due


@login_required
def reminders_json(request):
    """Interrogé toutes les minutes par la page ouverte, pour qu'un rappel apparaisse sans
    rechargement. Lecture seule et limité à la famille de l'utilisateur."""
    family = _get_family(request)
    settings = FamilySettings.load(family)
    return JsonResponse({'reminders': _due_reminders(family, settings)})


@login_required
@require_POST
def snooze_reminder(request):
    """« Plus tard » : met le rappel en sourdine pour la journée en cours seulement — il
    repart de lui-même le lendemain, sans que personne ait à le réactiver."""
    family = _get_family(request)
    reminder = RoutineReminder.objects.filter(
        pk=request.POST.get('reminder_id'), family=family
    ).first()
    if reminder is None:
        return JsonResponse({'ok': False}, status=404)
    if not _can_act_on(request, reminder.person):
        raise PermissionDenied
    reminder.acked_on = _today()
    reminder.save(update_fields=['acked_on'])
    return JsonResponse({'ok': True})


@login_required
def stars_view(request):
    family = _get_family(request)
    settings = FamilySettings.load(family)
    milestone = max(1, settings.star_milestone)
    kids = [p for p in _family_people(settings) if p in ('fille', 'fils')]
    today = _today()

    trackers = []
    for kid in kids:
        stars, _ = KidStars.objects.get_or_create(family=family, person=kid)
        all_dates = list(StarAward.objects.filter(family=family, person=kid).order_by('date')
                          .values_list('date', flat=True))
        awarded_dates = set(all_dates)
        streak = 0
        cursor = today
        while cursor in awarded_dates:
            streak += 1
            cursor -= datetime.timedelta(days=1)

        in_cycle = stars.total % milestone
        # The grid is this cycle's progress toward the next surprise: one cell per completed
        # day since the last milestone, filled in the order they actually happened — not a
        # fixed calendar window, so a day the kid missed never leaves a permanent gap and a
        # day completed out of calendar order still just fills the next cell in line.
        current_cycle_dates = all_dates[-in_cycle:] if in_cycle else []
        cells = [{'date': d, 'filled': True} for d in current_cycle_dates]
        cells += [{'date': None, 'filled': False}] * (milestone - len(cells))

        trackers.append({
            'person': kid,
            'name': _person_label(kid, settings),
            'total': stars.total,
            'level': _level_for(stars.total, milestone),
            'in_cycle': in_cycle,
            'milestone': milestone,
            'streak': streak,
            'days': cells,
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
    """Resolves the Monday to work on from ?week=YYYY-MM-DD (absent or invalid → this week,
    so every screen keeps its previous default until navigation happens).

    Read by every week-scoped screen — semainier, menu, courses, préparation de semaine — so
    that choosing a week once carries across all of them instead of each one silently
    snapping back to the current week (and editing it by accident)."""
    week_param = request.GET.get('week') or request.POST.get('week')
    if week_param:
        try:
            return _monday_of(datetime.date.fromisoformat(week_param))
        except ValueError:
            pass
    return _monday_of(_today())


def _week_context(week_start):
    """Common display context for a chosen week: its Monday/Sunday, whether it's the current
    one, and the ?week= value to thread through links and redirects."""
    today_monday = _monday_of(_today())
    return {
        'week_start': week_start,
        'week_end': week_start + datetime.timedelta(days=6),
        'week_param': week_start.isoformat(),
        'week_prev': (week_start - datetime.timedelta(days=7)).isoformat(),
        'week_next': (week_start + datetime.timedelta(days=7)).isoformat(),
        'is_current_week': week_start == today_monday,
        'is_future_week': week_start > today_monday,
    }


def _redirect_keeping(request, view_name, week_start=None, **extra):
    """redirect() that preserves the context the user was working in: the chosen week, the
    wizard step when the action came from the préparation de semaine, and whatever extra
    param the caller names (the favourites filter, typically).

    Plain redirect() drops the whole query string, which is why submitting any form on the
    menu or the courses used to bounce you back to the current week — and, worse, made the
    next save land on that week instead of the one you were preparing."""
    params = {}
    if week_start is not None:
        params['week'] = week_start.isoformat()
    if request.GET.get('wizard') == '1':
        params['wizard'] = '1'
        if request.GET.get('step'):
            params['step'] = request.GET['step']
    params.update({k: v for k, v in extra.items() if v})
    url = reverse(view_name)
    return redirect(f"{url}?{urlencode(params)}" if params else url)


def _menu_redirect(request, week_start):
    return _redirect_keeping(request, 'menu', week_start,
                             favoris='1' if request.GET.get('favoris') == '1' else None)


@login_required
def week_view(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    settings = FamilySettings.load(family)
    activities = list(Activity.objects.filter(family=family))
    people = _family_people(settings)

    week_start = _week_start_from_request(request)
    today_monday = _monday_of(_today())
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
            for x in tasks_for(p, d, settings, activities, holiday, False, day_mode=day_mode,
                               date=real_date):
                if x['id'] in MENAGE_DAILY_IDS or pillar_for(x['id'], x['period']) != 'menage':
                    continue
                short = DEEP_CLEAN_ROOMS[d] if x['id'] == 'deepclean' else MENAGE_SHORT_LABELS.get(x['id'], x['label'])
                by_label.setdefault(short, []).append(_person_label(p, settings))
        menage_cells.append([f"{' & '.join(names)} : {label}" for label, names in by_label.items()])

        # Même règle de sélection que partout ailleurs (task_logic.activities_on) : un
        # événement ponctuel n'apparaît qu'à sa date, un récurrent chaque semaine.
        day_activities = [a for a in activities_on(activities, real_date) if a.person in people]
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

    wizard = _wizard_banner(
        request, 1, 'Événements de la semaine',
        "Vérifiez les activités et les éventuels chevauchements avant de répartir les tâches.",
        next_url=f"{reverse('settings')}?wizard=1&step=2",
    )

    return render(request, 'planner/week.html', {
        'day_headers': day_headers, 'table_rows': table_rows, 'rotation_note': rotation_note,
        'week_start': week_start, 'week_end': week_start + datetime.timedelta(days=6),
        'prev_week': week_start - datetime.timedelta(days=7),
        'next_week': week_start + datetime.timedelta(days=7),
        'is_current_week': week_start == today_monday, 'current_week': today_monday,
        'week_note': settings.week_note, 'wizard': wizard,
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
        source_week = _monday_of(_today())
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
    week_start = _week_start_from_request(request)
    # Les articles issus du menu sont rattachés à leur semaine : on affiche ceux de la
    # semaine choisie, plus tous les produits sans semaine (habituels et ajouts manuels),
    # qui restent valables quelle que soit la semaine préparée.
    items = GroceryItem.objects.filter(family=family).filter(
        models.Q(week_start=week_start) | models.Q(week_start__isnull=True)
    )
    other_week_count = GroceryItem.objects.filter(family=family, week_start__isnull=False).exclude(
        week_start=week_start
    ).count()
    grouped = {}
    for i in items:
        grouped.setdefault(i.category or 'Ajoutés', []).append(i)
    wizard = _wizard_banner(
        request, 4, 'Courses',
        "Cochez les ingrédients au fur et à mesure de vos achats.",
        is_last=True,
    )
    return render(request, 'planner/maison.html', {
        'settings': settings, 'grouped': grouped, 'menu_category': MENU_GROCERY_CATEGORY,
        'wizard': wizard, 'other_week_count': other_week_count, **_week_context(week_start),
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
    return _redirect_keeping(request, 'maison', _week_start_from_request(request))


@login_required
@require_POST
def reset_grocery(request):
    family = _get_family(request)
    GroceryItem.objects.filter(family=family).update(checked=False)
    messages.success(request, "Liste de courses réinitialisée.")
    return _redirect_keeping(request, 'maison', _week_start_from_request(request))


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
        return _redirect_keeping(request, 'maison', _week_start_from_request(request))
    item.name = name
    item.category = request.POST.get('category', '').strip()
    qty_raw = request.POST.get('quantity', '').strip()
    if qty_raw:
        try:
            item.quantity = Decimal(qty_raw.replace(',', '.'))
        except InvalidOperation:
            messages.error(request, "Quantité invalide.")
            return _redirect_keeping(request, 'maison', _week_start_from_request(request))
    else:
        item.quantity = None
    item.unit = request.POST.get('unit', '').strip()
    item.save()
    messages.success(request, "Article modifié.")
    return _redirect_keeping(request, 'maison', _week_start_from_request(request))


@login_required
@require_POST
def delete_grocery(request):
    """Deletes a grocery item outright (Lot 4, point 4) — separate from toggle_grocery,
    which only checks/unchecks it."""
    family = _get_family(request)
    GroceryItem.objects.filter(pk=request.POST.get('item_id'), family=family).delete()
    messages.success(request, "Article supprimé.")
    return _redirect_keeping(request, 'maison', _week_start_from_request(request))


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
    return _redirect_keeping(request, 'maison', _week_start_from_request(request))


@login_required
def menu(request):
    family = _get_family(request)
    _ensure_seed_data(family)
    week_start = _week_start_from_request(request)
    # Unfiltered — used for the per-day recipe dropdown, which must always offer every
    # recipe regardless of the "favoris" display filter below.
    recipes = Recipe.objects.filter(family=family).order_by('-is_favorite', 'category', 'name')
    favoris_only = request.GET.get('favoris') == '1'
    display_recipes = recipes.filter(is_favorite=True) if favoris_only else recipes
    by_cat = {}
    for r in display_recipes:
        by_cat.setdefault(r.category, []).append(r)

    settings = FamilySettings.load(family)
    entries = {
        e.day: e for e in
        WeeklyMenuEntry.objects.filter(family=family, week_start=week_start).select_related('recipe')
    }
    # Un jour « restes » ne cuisine rien et n'achète rien : il ne pèse donc pas dans les
    # courses. Les portions réellement prévues, elles, comptent — c'est ce qui fait que
    # cuisiner pour deux repas achète bien pour deux repas.
    portions = [
        (e.recipe, e.servings_target(settings.household_servings))
        for e in entries.values() if e.recipe and not e.is_leftovers()
    ]
    all_ingredients = Recipe.aggregate_scaled(portions)
    for ing in all_ingredients:
        qty_text = format_quantity(ing['quantity'])
        ing['quantity_display'] = f"{qty_text} {ing['unit']}".strip() if qty_text else ing['unit']

    day_rows = []
    for d in DAYS:
        entry = entries.get(d)
        day_rows.append({
            'day': d, 'label': DAY_FULL[d],
            'selected': entry.recipe_id if entry else None,
            'servings': entry.servings if entry else None,
            'servings_target': entry.servings_target(settings.household_servings) if entry else settings.household_servings,
            'leftovers_from': entry.leftovers_from if entry else '',
            'leftovers_label': DAY_FULL.get(entry.leftovers_from, '') if entry else '',
            'recipe': entry.recipe if entry else None,
            'other_days': [(o, DAY_FULL[o]) for o in DAYS if o != d],
        })
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
                return _menu_redirect(request, week_start)
            recipe_form = form
        elif 'delete_recipe' in request.POST:
            Recipe.objects.filter(id=request.POST['delete_recipe'], family=family).delete()
            messages.success(request, "Recette supprimée.")
            return _menu_redirect(request, week_start)
        elif 'set_day' in request.POST:
            day = request.POST['set_day']
            recipe_id = request.POST.get('recipe_id') or None
            if day not in DAYS:
                messages.error(request, "Jour invalide.")
                return _menu_redirect(request, week_start)
            if recipe_id and not Recipe.objects.filter(id=recipe_id, family=family).exists():
                messages.error(request, "Recette invalide.")
                return _menu_redirect(request, week_start)
            # Choisir un plat lève les restes : les deux ne peuvent pas cohabiter.
            WeeklyMenuEntry.objects.update_or_create(
                family=family, week_start=week_start, day=day,
                defaults={'recipe_id': recipe_id, 'leftovers_from': ''},
            )
            return _menu_redirect(request, week_start)
        elif 'set_servings' in request.POST:
            day = request.POST.get('day')
            raw = request.POST.get('servings', '').strip()
            if day not in DAYS:
                messages.error(request, "Jour invalide.")
                return _menu_redirect(request, week_start)
            try:
                servings = int(raw) if raw else None
            except ValueError:
                messages.error(request, "Indiquez un nombre de personnes.")
                return _menu_redirect(request, week_start)
            if servings is not None and not 1 <= servings <= 50:
                messages.error(request, "Le nombre de personnes doit être compris entre 1 et 50.")
                return _menu_redirect(request, week_start)
            WeeklyMenuEntry.objects.update_or_create(
                family=family, week_start=week_start, day=day,
                defaults={'servings': servings},
            )
            return _menu_redirect(request, week_start)
        elif 'cook_double' in request.POST:
            # « Cuisiner pour deux repas » : on double les portions du jour cuisiné et on
            # marque le jour cible comme restes. Une seule action pour ce que les familles
            # font vraiment, au lieu de deux réglages à retrouver séparément.
            day = request.POST.get('day')
            target = request.POST.get('leftovers_day')
            if day not in DAYS or target not in DAYS or day == target:
                messages.error(request, "Choisissez un autre jour pour les restes.")
                return _menu_redirect(request, week_start)
            entry = WeeklyMenuEntry.objects.filter(
                family=family, week_start=week_start, day=day
            ).select_related('recipe').first()
            if entry is None or not entry.recipe:
                messages.error(request, "Choisissez d'abord un plat pour ce jour.")
                return _menu_redirect(request, week_start)
            base = entry.servings_target(settings.household_servings)
            entry.servings = base * 2
            entry.save(update_fields=['servings'])
            WeeklyMenuEntry.objects.update_or_create(
                family=family, week_start=week_start, day=target,
                defaults={'recipe': None, 'servings': None, 'leftovers_from': day},
            )
            messages.success(
                request,
                f"{DAY_FULL[day]} cuisiné pour {base * 2} personnes, {DAY_FULL[target]} en restes.",
            )
            return _menu_redirect(request, week_start)
        elif 'clear_leftovers' in request.POST:
            day = request.POST.get('day')
            if day in DAYS:
                WeeklyMenuEntry.objects.filter(
                    family=family, week_start=week_start, day=day
                ).update(leftovers_from='')
            return _menu_redirect(request, week_start)
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
            # On ne réutilise que les articles de cette semaine ou les produits habituels
            # (week_start NULL) : la liste d'une autre semaine n'est jamais écrasée.
            existing_by_name = {
                i.name: i for i in GroceryItem.objects.filter(family=family, name__in=names)
                .filter(models.Q(week_start=week_start) | models.Q(week_start__isnull=True))
            }
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
                            quantity=ing['quantity'], unit=ing['unit'], week_start=week_start,
                        )
                    else:
                        item.quantity = ing['quantity']
                        item.unit = ing['unit']
                        # Un ajout manuel (week_start NULL) réclamé par le menu devient un
                        # article de la semaine ; il n'est jamais supprimé pour autant.
                        item.week_start = week_start
                        if resolution == 'uncheck' and item.checked:
                            item.checked = False
                        item.save(update_fields=['quantity', 'unit', 'checked', 'week_start'])
                messages.success(request, "Ingrédients ajoutés à la liste de courses.")
                # Step 4 of the "Préparer notre semaine" wizard (see wizard_start): this POST
                # *is* step 4's action, so on success it hands off straight to 'maison' (the
                # screen that lists what was just copied) instead of looping back to 'menu' —
                # outside the wizard, behavior is unchanged (back to 'menu').
                if request.GET.get('wizard') == '1' and request.GET.get('step') == '4':
                    return _redirect_keeping(request, 'maison', week_start)
                return _menu_redirect(request, week_start)

    wizard_step = 4 if request.GET.get('step') == '4' else 3
    if wizard_step == 4:
        wizard = _wizard_banner(
            request, 4, 'Courses',
            "Ajoutez les ingrédients du menu à la liste de courses avec le bouton ci-dessous.",
            next_url=f"{reverse('maison')}?wizard=1&step=4&week={week_start.isoformat()}",
        )
    else:
        wizard = _wizard_banner(
            request, 3, 'Menus',
            "Choisissez une recette pour chaque jour de la semaine.",
            next_url=f"{reverse('menu')}?wizard=1&step=4&week={week_start.isoformat()}",
        )

    return render(request, 'planner/menu.html', {
        'by_cat': by_cat, 'day_rows': day_rows, 'recipes': recipes,
        'all_ingredients': all_ingredients, 'recipe_form': recipe_form,
        'favoris_only': favoris_only, 'pending_conflicts': pending_conflicts, 'wizard': wizard,
        'settings': settings, 'household_servings': settings.household_servings,
        **_week_context(week_start),
    })


@login_required
def cook_mode(request, pk):
    """Mode cuisine : une recette, une étape à la fois, en grand.

    Pensé pour un plan de travail : gros caractères, une seule étape visible, et les
    ingrédients déjà ramenés au nombre de personnes prévu — on ne veut pas faire une règle
    de trois les mains dans la farine. Le nombre de couverts vient du repas planifié quand
    on arrive depuis le menu, du réglage de la famille sinon, et reste ajustable ici.

    Lecture seule : cet écran ne modifie ni la recette ni le menu. L'avancement dans les
    étapes vit dans la page, volontairement — une étape cochée n'a pas de sens le lendemain,
    et personne n'a envie de « réinitialiser la recette » avant de cuisiner."""
    family = _get_family(request)
    recipe = get_object_or_404(Recipe, pk=pk, family=family)
    settings = FamilySettings.load(family)

    try:
        asked = int(request.GET.get('portions', ''))
    except (TypeError, ValueError):
        asked = None
    if asked is not None and not 1 <= asked <= 50:
        asked = None

    servings = asked
    if servings is None:
        day = request.GET.get('day')
        week_start = _week_start_from_request(request)
        entry = WeeklyMenuEntry.objects.filter(
            family=family, week_start=week_start, day=day, recipe=recipe
        ).first() if day in DAYS else None
        servings = (entry.servings_target(settings.household_servings) if entry
                    else settings.household_servings)

    ingredients = []
    for ing in recipe.scaled_ingredients(servings):
        qty_text = format_quantity(ing['quantity'])
        ingredients.append({
            'name': ing['name'],
            'quantity_display': f"{qty_text} {ing['unit']}".strip() if qty_text else ing['unit'],
        })

    return render(request, 'planner/cook.html', {
        'recipe': recipe,
        'servings': servings,
        'ingredients': ingredients,
        'steps': list(recipe.steps or []),
        'scaled': servings != recipe.servings,
        'back_url': f"{reverse('menu')}?week={_week_start_from_request(request).isoformat()}",
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
            freq, freq_error = _read_frequency(request)
            # Une tâche ponctuelle porte sa date : elle n'a pas besoin de jours de semaine.
            needs_days = freq is not None and freq['frequency'] != 'once'
            if freq_error:
                messages.error(request, freq_error)
            elif label and (days_selected or not needs_days):
                task = CustomTask.objects.create(
                    family=family,
                    person=request.POST.get('task_person', 'fille'),
                    days=days_selected,
                    period=request.POST.get('task_period', 'matin'),
                    label=label, **freq,
                )
                messages.success(request, f"Tâche ajoutée — {task.frequency_display()}.")
            else:
                messages.error(request, _custom_task_error(label, days_selected))
        elif 'save_rewards' in request.POST:
            try:
                milestone = int(request.POST.get('star_milestone', settings.star_milestone))
            except (TypeError, ValueError):
                milestone = settings.star_milestone
            settings.star_milestone = max(1, milestone)
            settings.star_reward_text = request.POST.get('star_reward_text', '').strip()
            settings.save()
            messages.success(request, "Récompense enregistrée.")
        elif 'add_checklist' in request.POST:
            name = request.POST.get('cl_name', '').strip()
            # Un élément par ligne : c'est ainsi qu'on recopie une liste déjà écrite ailleurs.
            items = [l.strip() for l in request.POST.get('cl_items', '').splitlines() if l.strip()]
            if not name:
                messages.error(request, "Donnez un nom à la checklist.")
            elif not items:
                messages.error(request, "Ajoutez au moins un élément, un par ligne.")
            else:
                Checklist.objects.create(
                    family=family, name=name, items=items,
                    person=request.POST.get('cl_person', 'fille'),
                    period=request.POST.get('cl_period', 'matin'),
                )
                messages.success(
                    request,
                    f"Checklist « {name} » enregistrée ({len(items)} élément{'s' if len(items) > 1 else ''}).",
                )
        elif 'add_reminder' in request.POST:
            person = request.POST.get('rem_person', 'fille')
            days_selected = [d for d in request.POST.getlist('rem_days') if d in DAYS]
            at_time = parse_free_time(request.POST.get('rem_time', ''))
            phase = request.POST.get('rem_phase', 'matin')
            # Les rappels sont réservés aux enfants : on le vérifie ici, pas seulement dans
            # la liste déroulante du formulaire.
            if person not in _kids_people(settings):
                messages.error(request, "Les rappels ne concernent que les routines des enfants.")
            elif phase not in dict(REMINDER_PHASE_CHOICES):
                messages.error(request, "Moment de la journée invalide.")
            elif at_time is None:
                messages.error(request, "Indiquez une heure pour le rappel (par exemple 7h30).")
            elif not days_selected:
                messages.error(request, "Choisissez au moins un jour pour ce rappel.")
            else:
                RoutineReminder.objects.create(
                    family=family, person=person, phase=phase, at_time=at_time,
                    days=days_selected, label=request.POST.get('rem_label', '').strip(),
                )
                messages.success(
                    request,
                    f"Rappel ajouté à {at_time:%H:%M} — {_days_summary(days_selected)}.",
                )
        elif 'save_reminder_settings' in request.POST:
            quiet_start = parse_free_time(request.POST.get('quiet_start', ''))
            quiet_end = parse_free_time(request.POST.get('quiet_end', ''))
            settings.reminders_enabled = 'reminders_enabled' in request.POST
            if quiet_start is not None:
                settings.quiet_start = quiet_start
            if quiet_end is not None:
                settings.quiet_end = quiet_end
            settings.save()
            messages.success(request, "Réglages des rappels enregistrés.")
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
            try:
                household = int(request.POST.get('household_servings', settings.household_servings))
                if 1 <= household <= 50:
                    settings.household_servings = household
            except (TypeError, ValueError):
                pass
            settings.save()
            messages.success(request, "Réglages enregistrés.")
        return _wizard_redirect(request, 'settings')

    activities = list(Activity.objects.filter(family=family))
    for a in activities:
        a.person_name = _person_label(a.person, settings)
    custom_tasks = list(CustomTask.objects.filter(family=family))
    for c in custom_tasks:
        c.person_name = _person_label(c.person, settings)
    members = FamilyMembership.objects.filter(family=family).select_related('user')
    tablet_url = request.build_absolute_uri(reverse('tablet', args=[settings.tablet_token])) if settings.tablet_token else ''
    checklists = list(Checklist.objects.filter(family=family))
    for cl in checklists:
        cl.person_name = _person_label(cl.person, settings)
    reminders = list(RoutineReminder.objects.filter(family=family))
    for r in reminders:
        r.person_name = _person_label(r.person, settings)
        r.effective_label = r.label or r.default_label(r.person_name)
        r.days_summary = _days_summary(r.days)
    task_exceptions = list(TaskException.objects.filter(family=family, active=True).order_by('-date'))
    for exc in task_exceptions:
        exc.person_name = _person_label(exc.person, settings)
        exc.reassigned_to_name = _person_label(exc.reassigned_to, settings) if exc.reassigned_to else ''
    wizard = _wizard_banner(
        request, 2, 'Répartition des tâches',
        "Ajustez les tâches personnalisées, les exceptions et les réattributions de la semaine.",
        next_url=f"{reverse('menu')}?wizard=1&step=3",
    )
    return render(request, 'planner/settings.html', {
        'settings': settings, 'activities': activities, 'custom_tasks': custom_tasks,
        'days': DAYS, 'day_full': DAY_FULL, 'members': members, 'tablet_url': tablet_url,
        # Raccourcis du sélecteur de jours : la liste vient de task_logic, jamais du template.
        'school_days_csv': ','.join(SCHOOL_DAYS), 'weekend_days_csv': ','.join(WEEKEND_DAYS),
        'task_exceptions': task_exceptions, 'wizard': wizard,
        'reminders': reminders, 'reminder_phases': REMINDER_PHASE_CHOICES,
        'checklists': checklists, 'frequencies': CUSTOM_TASK_FREQUENCIES,
        'monthly_choices': MONTHLY_NTH_CHOICES, 'phases': PHASE_CHOICES,
        'all_people': [(p, _person_label(p, settings)) for p in _family_people(settings)],
        'today_iso': _today().isoformat(),
        'kid_people': [(p, _person_label(p, settings)) for p in _kids_people(settings)],
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


@login_required
@parent_required
@require_POST
def apply_checklist(request, pk):
    """Transforme une checklist en vraies tâches personnalisées.

    Une checklist est un modèle : l'appliquer crée une CustomTask par élément, avec la
    personne, le moment et la fréquence choisis au moment de l'application. Les tâches
    créées vivent ensuite leur vie — on les réordonne, on les décale, on les supprime comme
    n'importe quelle autre, et modifier la checklist plus tard ne les retouche pas.

    Un élément déjà présent à l'identique (même personne, même intitulé) est passé : on
    peut réappliquer une liste sans se retrouver avec des doublons."""
    family = _get_family(request)
    checklist = Checklist.objects.filter(pk=pk, family=family).first()
    if checklist is None:
        messages.error(request, "Checklist introuvable.")
        return redirect('settings')

    person = request.POST.get('apply_person', checklist.person)
    if person not in PERSON_KEYS:
        messages.error(request, "Personne invalide.")
        return redirect('settings')
    period = request.POST.get('apply_period', checklist.period)
    if period not in dict(PHASE_CHOICES):
        period = checklist.period
    days_selected = [d for d in request.POST.getlist('apply_days') if d in DAYS]
    freq, freq_error = _read_frequency(request, prefix='apply')
    if freq_error:
        messages.error(request, freq_error)
        return redirect('settings')
    if freq['frequency'] != 'once' and not days_selected:
        messages.error(request, "Choisissez au moins un jour pour appliquer cette checklist.")
        return redirect('settings')

    existing = set(
        CustomTask.objects.filter(family=family, person=person)
        .values_list('label', flat=True)
    )
    created = 0
    for label in checklist.items_list():
        if label in existing:
            continue
        CustomTask.objects.create(
            family=family, person=person, days=days_selected,
            period=period, label=label, **freq,
        )
        existing.add(label)
        created += 1

    skipped = checklist.item_count() - created
    if created:
        message = f"{created} tâche{'s' if created > 1 else ''} ajoutée{'s' if created > 1 else ''} depuis « {checklist.name} »."
        if skipped:
            message += f" {skipped} déjà présente{'s' if skipped > 1 else ''}, non dupliquée{'s' if skipped > 1 else ''}."
        messages.success(request, message)
    else:
        messages.success(request, f"Tout « {checklist.name} » était déjà en place, rien ajouté.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def delete_checklist(request, pk):
    Checklist.objects.filter(pk=pk, family=_get_family(request)).delete()
    messages.success(request, "Checklist supprimée.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def delete_reminder(request, pk):
    RoutineReminder.objects.filter(pk=pk, family=_get_family(request)).delete()
    messages.success(request, "Rappel supprimé.")
    return redirect('settings')


@login_required
@parent_required
@require_POST
def toggle_reminder(request, pk):
    """Met un rappel en pause sans le supprimer — on garde l'horaire pour plus tard plutôt
    que d'obliger à le ressaisir à chaque vacances scolaires."""
    reminder = RoutineReminder.objects.filter(pk=pk, family=_get_family(request)).first()
    if reminder:
        reminder.active = not reminder.active
        reminder.save(update_fields=['active'])
        messages.success(request, "Rappel réactivé." if reminder.active else "Rappel mis en pause.")
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
    today_idx = _today().weekday()
    if day not in DAYS:
        day = DAYS[today_idx]
    real_date = _today() + datetime.timedelta(days=(DAYS.index(day) - today_idx))
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

    week_start = _monday_of(_today())
    menu_entry = WeeklyMenuEntry.objects.filter(
        family=family, week_start=week_start, day=day
    ).select_related('recipe').first()
    dinner = menu_entry.recipe if menu_entry else None

    # Les 7 prochains jours réels, via la même règle que les autres écrans : un événement
    # ponctuel n'est listé qu'à sa date, jamais reconduit chaque semaine (l'ancien tri par
    # écart de jour de semaine le faisait réapparaître indéfiniment).
    upcoming = []
    for offset in range(7):
        d = real_date + datetime.timedelta(days=offset)
        for a in sorted(activities_on(activities, d), key=lambda x: x.start_time or datetime.time.max):
            a.person_name = _person_label(a.person, settings)
            a.day_label = "aujourd'hui" if offset == 0 else DAY_FULL[DAYS[d.weekday()]]
            upcoming.append(a)
    upcoming = upcoming[:8]

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
        freq, freq_error = _read_frequency(request)
        needs_days = freq is not None and freq['frequency'] != 'once'
        if freq_error:
            messages.error(request, freq_error)
        elif label and (days_selected or not needs_days):
            task.label = label
            task.person = request.POST.get('task_person', task.person)
            task.period = request.POST.get('task_period', task.period)
            task.days = days_selected
            for field, value in freq.items():
                setattr(task, field, value)
            task.save()
            messages.success(request, f"Tâche modifiée — {task.frequency_display()}.")
        else:
            messages.error(request, _custom_task_error(label, days_selected))
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


@login_required
def wizard_start(request):
    """Entry point AND final summary screen for the "Préparer notre semaine" wizard (Lot 5b).

    Design: the wizard is a pure navigation layer over the 4 already-existing screens
    (week_view → settings_view → menu → maison) — it stores no progress anywhere. The single
    ?wizard=1&step=N pair carried on each screen's URL *is* the progress state (see
    _wizard_banner/_wizard_redirect above); this view itself only ever renders one of two
    static cards (the intro with "Commencer", or the "Terminé !" recap when ?done=1), and its
    own URL never carries a step. Consequences of that choice, spelled out per the task brief:
      - Nothing to persist, no migration: refreshing, bookmarking or sharing a step URL just
        re-derives the same banner from the query string.
      - Leaving the flow needs no cleanup: navigate anywhere without ?wizard=1 (e.g. via the
        tab bar) and that screen renders exactly as it does outside the wizard — the banner
        included on it simply won't render since its own `wizard` context var comes back None.
      - Step order, decided here: Événements (week_view) → Répartition des tâches
        (settings_view: tâches personnalisées + exceptions/réattributions already listed
        there) → Menus (menu) → Courses (menu's copy_to_courses action, which then hands off
        to maison — see the 'wizard' branch inside menu()).

    Parent-only: step 2 lands on settings_view (parent_required) and step 4 depends on
    parent-only exception/reassignment tools from step 2, so a non-parent starting the wizard
    would hit a 403 partway through — refused up front instead, same pattern as the
    parent-only "Gérer les tâches" toggle on 'Aujourd'hui'."""
    if not _is_parent(request):
        raise PermissionDenied
    family = _get_family(request)
    settings = FamilySettings.load(family)
    week_start = _week_start_from_request(request)
    steps = _week_preparation(family, settings, week_start)
    ready = [s for s in steps if s['state'] == 'ok']
    context = _week_context(week_start)
    context.update({
        'steps': steps,
        'ready_count': len(ready),
        'all_ready': len(ready) == len(steps),
        'state_labels': WEEK_STATE_LABELS,
        'start_url': f"{reverse('week')}?wizard=1&step=1&week={week_start.isoformat()}",
    })
    return render(request, 'planner/wizard.html', context)
