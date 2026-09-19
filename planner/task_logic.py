"""
Pure functions that generate each family member's task list for a given day.
This mirrors the logic of the original single-file prototype, ported to Python.
"""
import datetime
import re

DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
DAY_FULL = {'lundi': 'Lundi', 'mardi': 'Mardi', 'mercredi': 'Mercredi', 'jeudi': 'Jeudi',
            'vendredi': 'Vendredi', 'samedi': 'Samedi', 'dimanche': 'Dimanche'}
SCHOOL_DAYS = ['lundi', 'mardi', 'jeudi', 'vendredi']
WEEKEND_DAYS = ['samedi', 'dimanche']
DEEP_CLEAN_ROOMS = {'lundi': 'Salon', 'mardi': 'Salle de bain', 'mercredi': 'Chambre parents',
                     'jeudi': 'Chambre des enfants', 'vendredi': 'Entrée & couloir'}


def next_day(day):
    i = DAYS.index(day)
    return DAYS[(i + 1) % 7]


def is_school_day(day):
    return day in SCHOOL_DAYS


def is_weekend_day(day):
    return day in WEEKEND_DAYS


def is_bureau_day(day, settings):
    return day in SCHOOL_DAYS and day != settings.tt2_day


# Vacances scolaires — Zone B (source : education.gouv.fr, calendrier 2026-2027 publié
# le 23/10/2025). Chaque tuple est (label, début inclus, fin incluse — la veille de la
# reprise des cours, pas le jour de reprise lui-même). Été 2027 : la rentrée 2027-2028
# n'est pas encore publiée ; borne de fin volontairement prudente (toujours largement
# dans les vacances réelles) — à ajuster dès que le calendrier suivant sort.
ZONE_B_HOLIDAYS = [
    ('Toussaint', datetime.date(2026, 10, 17), datetime.date(2026, 11, 1)),
    ('Noël', datetime.date(2026, 12, 19), datetime.date(2027, 1, 3)),
    ('Hiver', datetime.date(2027, 2, 20), datetime.date(2027, 3, 7)),
    ('Printemps', datetime.date(2027, 4, 17), datetime.date(2027, 5, 2)),
    ('Été', datetime.date(2027, 7, 3), datetime.date(2027, 8, 31)),
]


def is_zone_b_holiday(date):
    return any(start <= date <= end for _, start, end in ZONE_B_HOLIDAYS)


def occurs_on(activity, date):
    """Single rule deciding whether an Activity happens on a given calendar date, shared by
    every screen (Aujourd'hui, semainier, tablette, génération des tâches, bilan de semaine).

    A one-off (specific_date set) happens on that date and nowhere else — in particular it
    never also shows up on its weekly `day` slot. A recurring activity happens every week on
    its `day`. Callers that genuinely have no date in hand pass date=None, which keeps the
    weekly behaviour and skips one-offs entirely (they can't be placed without a date)."""
    if date is None:
        return not activity.specific_date
    if activity.specific_date:
        return activity.specific_date == date
    return activity.day == DAYS[date.weekday()]


def activities_on(activities, date):
    """Every activity of the family happening on `date`, one-offs included — the list the
    semainier, the tablette and the week-preparation bilan all read from."""
    return [a for a in activities if occurs_on(a, date)]


def activities_for(person, day, activities, date=None):
    """That person's activities for a day. `date` makes the selection date-aware (one-offs
    on their own date only); without it, only recurring activities of that weekday match."""
    return [a for a in activities
            if a.person == person and occurs_on(a, date) and (date is not None or a.day == day)]


def phase_for_time(value):
    """Maps a time of day to one of the three routine phases (see group_by_phase): before
    12h00 = matin, 12h00–18h00 = journée, 18h00 et après = soir. Used both to slot a timed
    activity into the right phase and to decide which phase card opens by default on
    'Aujourd'hui' — one rule, so the two never drift apart."""
    if value is None:
        return 'soir'
    if value < datetime.time(12, 0):
        return 'matin'
    if value < datetime.time(18, 0):
        return 'journee'
    return 'soir'


def days_summary(days):
    """Human recap of a recurrence's weekdays, for flashes and settings rows. Recognises the
    same three shortcuts as the day picker (tous les jours / jours d'école / week-end) so the
    wording matches what was clicked.

    Vit ici plutôt que dans views : les modèles s'en servent aussi pour décrire une tâche,
    et une phrase qui décrit une règle doit être au même endroit que la règle."""
    ordered = [d for d in DAYS if d in (days or [])]
    if not ordered:
        return 'aucun jour'
    if len(ordered) == len(DAYS):
        return 'tous les jours'
    if set(ordered) == set(SCHOOL_DAYS):
        return "les jours d'école"
    if set(ordered) == set(WEEKEND_DAYS):
        return 'le week-end'
    return ', '.join(ordered)


def nth_weekday_of_month(date):
    """Rang de cette date parmi les mêmes jours de la semaine de son mois : le 3e mardi du
    mois renvoie 3. Sert à la fréquence mensuelle."""
    return (date.day - 1) // 7 + 1


def is_last_weekday_of_month(date):
    """True si aucune autre date du mois ne partage ce jour de la semaine après celle-ci —
    « le dernier samedi du mois », qui tombe le 4e ou le 5e selon les mois."""
    return (date + datetime.timedelta(days=7)).month != date.month


def custom_task_occurs_on(task, date):
    """Règle unique : cette tâche personnalisée a-t-elle lieu à cette date ?

    Une seule fonction pour les quatre fréquences, afin qu'aucun écran ne puisse en appliquer
    une variante à lui. `days` dit toujours quels jours de la semaine ; la fréquence dit
    quelles semaines parmi celles-là.

    Sans date (les appels historiques qui ne raisonnent qu'en jour de semaine), on ne peut
    juger que de l'hebdomadaire : les autres fréquences ont besoin d'une date réelle et
    répondent False plutôt que de s'afficher tous les jours par défaut."""
    frequency = getattr(task, 'frequency', 'weekly') or 'weekly'

    if frequency == 'once':
        return bool(task.specific_date) and date is not None and task.specific_date == date

    if date is None:
        return frequency == 'weekly'

    day = DAYS[date.weekday()]
    if day not in (task.days or []):
        return False

    if frequency == 'weekly':
        return True

    if frequency == 'biweekly':
        anchor = task.anchor_week
        if not anchor:
            return True   # sans référence, on ne saute rien : mieux vaut trop que rien
        monday = date - datetime.timedelta(days=date.weekday())
        anchor_monday = anchor - datetime.timedelta(days=anchor.weekday())
        return ((monday - anchor_monday).days // 7) % 2 == 0

    if frequency == 'monthly':
        nth = task.monthly_nth or 1
        if nth == -1:
            return is_last_weekday_of_month(date)
        return nth_weekday_of_month(date) == nth

    return True


def find_schedule_conflicts(day_activities):
    """Flags Activity rows (any iterable exposing .id, .person, .accompanied_by,
    .start_time, .end_time) that clash with another activity the same day: either the
    same person is double-booked, or the same escort (accompanied_by) would need to be
    in two places at once. Returns the set of ids involved in at least one clash — display
    only, no automatic resolution. Activities missing start_time or end_time can't be
    compared for overlap and are simply skipped."""
    timed = [a for a in day_activities if a.start_time and a.end_time]
    conflicting_ids = set()
    for i in range(len(timed)):
        for j in range(i + 1, len(timed)):
            a, b = timed[i], timed[j]
            if a.start_time >= b.end_time or b.start_time >= a.end_time:
                continue  # no time overlap
            same_person = a.person == b.person
            same_escort = a.accompanied_by and a.accompanied_by == b.accompanied_by
            if same_person or same_escort:
                conflicting_ids.add(a.id)
                conflicting_ids.add(b.id)
    return conflicting_ids


_FREE_TIME_RE = re.compile(r'(\d{1,2})\s*[h:]\s*(\d{2})?')


def parse_free_time(text):
    """Best-effort parse of a freeform time string (e.g. '17h30', '17:30', '9h') into a
    datetime.time, for the settings 'add activity' form which still collects a single
    text field pending the routines-v2 UI rework. Returns None if nothing looks like a time."""
    if not text:
        return None
    match = _FREE_TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime.time(hour, minute)


def douche_today(kid, day, activities, date=None):
    sport = len(activities_for(kid, day, activities, date)) > 0
    idx = DAYS.index(day)
    alternate = idx % 2 == 0  # lundi, mercredi, vendredi
    return sport or alternate


def _own_activity_tasks(person, day, activities, date=None):
    """A person's own scheduled activities (sport, etc.) as checkable tasks — separate from
    _kid_activity_drive_tasks, which is an adult driving a *kid* to theirs.

    Task ids key on the Activity's primary key, not its position in the list: deleting or
    reordering an activity used to shift every following id (activite0, activite1…) and
    silently re-attach an old TaskCompletion to a different activity. The phase comes from
    the start time, so a morning activity lands in the morning routine."""
    tasks = []
    for a in activities_for(person, day, activities, date):
        time_label = a.time_range_label()
        label = a.label + (f" ({time_label})" if time_label else '')
        tasks.append(t(f'activite_{a.id}', label, phase_for_time(a.start_time)))
    return tasks


def t(task_id, label, period='', info=False):
    return {'id': task_id, 'label': label, 'period': period, 'info': info}


MENAGE_IDS = {'lv_vide', 'rangertable_m', 'panierSDB', 'linge', 'mettre_table',
              'debarrasser_table', 'lv_remplit', 'chambre', 'panier', 'frigo', 'draps',
              'reset', 'lessive', 'cuisine', 'deepclean', 'menage'}
REPAS_IDS = {'petitdej', 'gouter', 'diner', 'petitdej_prep', 'petitdej_famille', 'repas',
             'gouter_pret', 'diner_famille', 'courses', 'mealprep', 'gouters',
             'lunchbox_demain', 'dejeuner'}
PILLAR_LABELS = {'matin': 'Matin', 'menage': 'Ménage', 'repas': 'Repas',
                  'journee': 'Journée', 'soir': 'Soir'}


PHASE_LABELS = {'matin': 'Routine du matin', 'journee': 'Activité / Journée', 'soir': 'Routine du soir'}


def group_by_phase(tasks):
    """Splits a person's already-chronological task list into 3 contiguous blocks
    (matin / journée / soir) for display — without reordering anything, since the list is
    already built matin-block first, then daytime info, then soir-block."""
    buckets = {'matin': [], 'journee': [], 'soir': []}
    for task in tasks:
        phase = task['period'] if task['period'] in ('matin', 'soir') else 'journee'
        buckets[phase].append(task)
    return [(p, PHASE_LABELS[p], buckets[p]) for p in ('matin', 'journee', 'soir') if buckets[p]]


def apply_order(tasks, order_map):
    """Resorts a phase's task list per a person's saved custom order (task_id -> rank).
    Tasks with a saved rank come first, in that rank's order; anything never explicitly
    ordered (new tasks, tasks only seen on other days) keeps its original relative order,
    appended after — so a fresh order never has to be set for everything at once."""
    def sort_key(pair):
        idx, task = pair
        rank = order_map.get(task['id'])
        return (0, rank, idx) if rank is not None else (1, 0, idx)
    return [task for idx, task in sorted(enumerate(tasks), key=sort_key)]


def active_day_mode(family, person, date):
    """Returns the DayMode.mode override for a family/person/date ('vacances', 'absence',
    'allegee'), or 'normal' if none is set. See tasks_for's `day_mode` kwarg for how this
    reshapes the generated task list."""
    from .models import DayMode
    day_mode = DayMode.objects.filter(family=family, person=person, date=date).first()
    return day_mode.mode if day_mode else 'normal'


# 'absence' (the person is away for the day): drop anything tied to being physically
# present somewhere — school, driving to/attending an activity, work — but keep the core
# spiritual/hygiene/meal routine (prayer, wudu, brushing teeth, showering, meals...), since
# that's reasonably still relevant wherever they are. Coran is deliberately NOT in this set —
# it's non-negotiable every day, with no exception for being away (see _apply_day_mode).
DAY_MODE_ABSENCE_DROP_IDS = {
    'ecole', 'devoirs', 'sac_demain', 'sac_semaine', 'activite_famille', 'arabe',
    'vacances', 'ecran', 'pasecole', 'mahlo', 'travail', 'pickup', 'pickup_midi',
    'accompagnement', 'journee',
}
# 'allegee' (lightened day — also the "sick day" mode; see DAY_MODE_CHOICES): drop heavy
# chores and full homework, keep the rest of the routine as-is.
DAY_MODE_ALLEGEE_DROP_IDS = {'devoirs', 'deepclean', 'lessive', 'frigo', 'draps', 'reset'}

# Le Coran a lieu tous les jours, sans exception, et n'est jamais retiré par un mode de
# journée : il n'est allégé que si l'enfant est malade (day_mode == 'allegee'). Les libellés
# sont ici, en un seul endroit, pour que cette règle métier ne puisse pas diverger d'un jour
# de la semaine à l'autre.
CORAN_FULL_LABEL = 'Coran — lecture & apprentissage (~1h)'
CORAN_SUNDAY_LABEL = 'Coran — lecture'
CORAN_LIGHT_LABEL = 'Coran — révision légère'


def _apply_day_mode(tasks, day_mode):
    """Drops non-essential tasks for a DayMode-marked day (see active_day_mode) — 'normal'
    and 'vacances' (already folded into holiday_today by tasks_for) leave the list
    untouched. Own scheduled activities and driving-to-activity tasks (dynamic ids like
    'activite0', 'drive_fille1') are always dropped on an 'absence' day, since they assume
    the person is present that day. Coran is never in either drop set (see
    DAY_MODE_ABSENCE_DROP_IDS) — on an 'allegee' day it's shortened instead of dropped."""
    if day_mode not in ('absence', 'allegee'):
        return tasks
    drop_ids = DAY_MODE_ABSENCE_DROP_IDS if day_mode == 'absence' else DAY_MODE_ALLEGEE_DROP_IDS

    def keep(task):
        if task['id'] in drop_ids:
            return False
        if day_mode == 'absence' and (task['id'].startswith('activite') or task['id'].startswith('drive_')):
            return False
        return True

    tasks = [task for task in tasks if keep(task)]
    if day_mode == 'allegee':
        for task in tasks:
            if task['id'] == 'coran':
                task['label'] = CORAN_LIGHT_LABEL
    return tasks


def split_by_exceptions(tasks, disabled_ids, not_applicable_ids):
    """Applies TaskException overrides to a generated task list: 'disabled' tasks are
    dropped entirely, 'not applicable' tasks are kept (still visible) but flagged so the
    completion/star calculation (see views._checkable_ids_for) can exclude them without
    counting them as undone."""
    result = []
    for task in tasks:
        if task['id'] in disabled_ids:
            continue
        result.append(dict(task, not_applicable=task['id'] in not_applicable_ids))
    return result


def pillar_for(task_id, period):
    """Classifies a task into one of the 4 organizing pillars (+ 'journee' for context/
    school/work info that doesn't belong to a routine or a chore)."""
    if task_id in MENAGE_IDS:
        return 'menage'
    if task_id in REPAS_IDS:
        return 'repas'
    if period == 'matin':
        return 'matin'
    if period == 'soir':
        return 'soir'
    return 'journee'


def build_morning(day, kid, settings, school_today=None):
    if school_today is None:
        school_today = is_school_day(day)
    tasks = [
        t('reveil', 'Réveil libre' if day == 'mercredi' else 'Réveil à 7h00', 'matin'),
        t('lit', 'Faire son lit', 'matin'),
        t('oudou_m', "Faire l'oudou", 'matin'),
        t('priere_m', 'Prière', 'matin'),
    ]
    if school_today:
        tasks.append(t('habillage', 'Habillage', 'matin'))
        tasks.append(t('pyjama', 'Plier son pyjama', 'matin'))
    else:
        tasks.append(t('pyjama_info', 'Reste en pyjama le matin', 'matin', info=True))
    tasks.append(t('petitdej', 'Petit-déjeuner', 'matin'))
    tasks.append(t('brossage_m', 'Brossage de dents', 'matin'))
    if settings.rotation_lave_vaisselle == kid:
        tasks.append(t('lv_vide', 'Vider le lave-vaisselle', 'matin'))
    else:
        tasks.append(t('rangertable_m', 'Ranger la table', 'matin'))
    return tasks


def build_evening_school_like(kid, day, settings, activities, holiday_tomorrow=False, date=None):
    sac_demain = is_school_day(next_day(day)) and not holiday_tomorrow
    fold_day = day in ('mercredi', 'vendredi')
    basket_day = day in ('mardi', 'jeudi')
    is_table = settings.rotation_table == kid
    is_lv = settings.rotation_lave_vaisselle == kid

    tasks = [
        t('chaussures', 'Ranger ses chaussures', 'soir'),
        t('affaires', 'Ranger ses affaires', 'soir'),
        t('oudou_s', "Faire l'oudou", 'soir'),
        t('priere1', 'Prière', 'soir'),
        t('gouter', 'Goûter sain', 'soir'),
        t('devoirs', 'Devoirs', 'soir'),
        t('coran', CORAN_FULL_LABEL, 'soir'),
    ]
    tasks += _own_activity_tasks(kid, day, activities, date)
    if douche_today(kid, day, activities, date):
        tasks.append(t('douche', 'Douche', 'soir'))
    if basket_day:
        tasks.append(t('panierSDB', 'Mettre son panier à linge dans la salle de bain', 'soir'))
    if fold_day:
        tasks.append(t('linge', 'Plier et ranger son linge propre', 'soir'))
    if is_table:
        tasks.append(t('mettre_table', 'Mettre la table', 'soir'))
    tasks.append(t('diner', 'Dîner', 'soir'))
    if is_table:
        tasks.append(t('debarrasser_table', 'Débarrasser la table', 'soir'))
    if is_lv:
        tasks.append(t('lv_remplit', 'Remplir le lave-vaisselle', 'soir'))
    if sac_demain:
        tasks.append(t('sac_demain', 'Faire son sac pour demain', 'soir'))
    tasks.append(t('temps_libre', 'Temps libre', 'soir'))
    tasks.append(t('brossage_s', 'Brossage de dents', 'soir'))
    tasks.append(t('priere2', 'Prière', 'soir'))
    tasks.append(t('coucher', 'Coucher à 20h30', 'soir'))
    return tasks


def _weekend_rotation_tasks(kid, settings):
    tasks = []
    if settings.rotation_table == kid:
        tasks.append(t('mettre_table', 'Mettre la table (dîner)', 'soir'))
        tasks.append(t('debarrasser_table', 'Débarrasser la table', 'soir'))
    if settings.rotation_lave_vaisselle == kid:
        tasks.append(t('lv_remplit', 'Remplir le lave-vaisselle', 'soir'))
    return tasks


def tasks_for_kid(kid, day, settings, activities, holiday_today=False, holiday_tomorrow=False, date=None):
    if day == 'samedi':
        tasks = [
            t('levee', 'Réveil tranquille', 'matin'),
            t('lit', 'Faire son lit', 'matin'),
            t('oudou_m', "Faire l'oudou", 'matin'),
            t('priere_m', 'Prière', 'matin'),
            t('pyjama_info', 'Reste en pyjama le matin', 'matin', info=True),
            t('petitdej', 'Petit-déjeuner', 'matin'),
            t('brossage_m', 'Brossage de dents', 'matin'),
        ]
        if settings.rotation_lave_vaisselle == kid:
            tasks.append(t('lv_vide', 'Vider le lave-vaisselle', 'matin'))
        else:
            tasks.append(t('rangertable_m', 'Ranger la table', 'matin'))
        tasks += [
            t('chambre', 'Ranger sa chambre', 'matin'),
            t('chaussures', 'Ranger ses chaussures', 'matin'),
            t('linge', 'Plier et ranger son linge propre', 'matin'),
            t('coran', CORAN_FULL_LABEL, 'journée'),
        ]
        tasks += _weekend_rotation_tasks(kid, settings)
        tasks += _own_activity_tasks(kid, day, activities, date)
        tasks.append(t('activite_famille', 'Activité en famille', 'après-midi', info=True))
        return tasks

    if day == 'dimanche':
        tasks = [
            t('lit', 'Faire son lit', 'matin'),
            t('oudou_m', "Faire l'oudou", 'matin'),
            t('priere_m', 'Prière', 'matin'),
            t('pyjama_info', 'Reste en pyjama le matin', 'matin', info=True),
            t('arabe', "Cours d'arabe (matin)", 'matin', info=True),
            t('petitdej', 'Petit-déjeuner', 'matin'),
            t('brossage_m', 'Brossage de dents', 'matin'),
        ]
        if settings.rotation_lave_vaisselle == kid:
            tasks.append(t('lv_vide', 'Vider le lave-vaisselle', 'matin'))
        else:
            tasks.append(t('rangertable_m', 'Ranger la table', 'matin'))
        tasks += [
            t('panier', 'Vider le panier à linge sale', 'matin'),
            t('sac_semaine', 'Préparer le cartable / les affaires de la semaine', 'journée'),
            t('coran', CORAN_SUNDAY_LABEL, 'journée'),
        ]
        tasks += _weekend_rotation_tasks(kid, settings)
        tasks += _own_activity_tasks(kid, day, activities, date)
        tasks.append(t('mahlo', "Chez Mahlo (fin d'après-midi)", 'après-midi', info=True))
        tasks.append(t('repos', 'Repos / temps libre', 'journée', info=True))
        return tasks

    # lundi/mardi/mercredi/jeudi/vendredi : un seul gabarit, école ou non selon le jour
    # ET les vacances scolaires (zone B) — le mercredi n'est jamais un jour d'école, et
    # n'importe quel jour peut tomber en vacances.
    school_today = is_school_day(day) and not holiday_today
    tasks = build_morning(day, kid, settings, school_today=school_today)
    if school_today:
        tasks.append(t('ecole', 'École (8h30 → 16h30)', 'journée', info=True))
    elif holiday_today:
        tasks.append(t('vacances', 'Vacances scolaires — journée libre', 'journée', info=True))
        tasks.append(t('ecran', "Temps d'écran autorisé (vacances)", 'journée', info=True))
    elif day == 'mercredi':
        tasks.append(t('pasecole', "Pas d'école — journée libre / activités", 'journée', info=True))
    return tasks + build_evening_school_like(kid, day, settings, activities, holiday_tomorrow=holiday_tomorrow, date=date)


def tasks_for_maman(day, settings, activities, date=None):
    is_tt = day == 'mercredi' or day == settings.tt2_day
    lunchbox_demain = is_bureau_day(next_day(day), settings)
    tasks = []
    if day == 'samedi':
        tasks = [
            t('courses', 'Courses de la semaine'),
            t('frigo', 'Laver le frigo'),
            t('draps', 'Changer les draps'),
            t('reset', 'Reset ménage général (rattraper ce qui traîne)'),
            t('famille', 'Activité en famille'),
            t('sport', 'Sport', 'matin'),
        ]
        tasks += _kid_activity_drive_tasks('maman', day, settings, activities, date)
        tasks += _own_activity_tasks('maman', day, activities, date)
    elif day == 'dimanche':
        tasks = [
            t('hammam', 'Hammam'),
            t('mealprep', 'Préparation des repas de la semaine (batch cooking)'),
            t('gouters', 'Préparer les goûters sains de la semaine'),
            t('sport', 'Sport', 'matin'),
            t('repos', 'Repos'),
        ]
        tasks += _kid_activity_drive_tasks('maman', day, settings, activities, date)
        tasks += _own_activity_tasks('maman', day, activities, date)
    else:
        tasks.append(t('priere_m', 'Prière', 'matin'))
        tasks.append(t('sport', 'Sport (idéalement avant le réveil des enfants)', 'matin'))
        if day in ('mercredi', 'vendredi'):
            tasks.append(t('lessive', 'Lancer une machine', 'matin'))
        if settings.maman_travaille:
            tasks.append(t('prepa', 'Se préparer pour le travail', 'matin'))
        tasks.append(t('petitdej_prep', 'Préparer le petit-déjeuner', 'matin'))
        tasks.append(t('petitdej_famille', 'Petit-déjeuner en famille', 'matin'))
        if settings.maman_travaille:
            tasks.append(t('travail', 'Télétravail — 9h à 17h' if is_tt else 'Travail au bureau — 9h à 17h', 'journée', info=True))
        if day == 'mercredi':
            tasks.append(t('accompagnement', 'Accompagner les activités des enfants', 'journée'))
        tasks.append(t('marche', 'Marche (15–20 min)', 'journée'))
        if day == settings.courses_day:
            tasks.append(t('courses', 'Courses de la semaine', 'journée'))
        tasks.append(t('douche_soir', 'Douche', 'soir'))
        tasks.append(t('priere_soir', 'Prière', 'soir'))
        tasks += _kid_activity_drive_tasks('maman', day, settings, activities, date)
        tasks += _own_activity_tasks('maman', day, activities, date)
        tasks.append(t('repas', 'Préparer le repas du soir', 'soir'))
        tasks.append(t('gouter_pret', 'Goûter sain des enfants prêt', 'soir'))
        tasks.append(t('diner_famille', 'Dîner en famille', 'soir'))
        tasks.append(t('cuisine', 'Ranger la cuisine', 'soir'))
        if DEEP_CLEAN_ROOMS.get(day):
            tasks.append(t('deepclean', 'Deep clean : ' + DEEP_CLEAN_ROOMS[day], 'soir'))
    if lunchbox_demain and settings.maman_travaille:
        tasks.append(t('lunchbox_demain', 'Préparer ma lunch box pour demain', 'soir'))
    return tasks


def _kid_names(settings):
    kids = [('fille', settings.fille_name)]
    if settings.nb_enfants == 2:
        kids.append(('fils', settings.fils_name))
    return kids


def _kid_activity_drive_tasks(person, day, settings, activities, date=None):
    """Drop-off / pick-up tasks for ONE adult: only the activities where that person is the
    designated accompanied_by (drop-off) or picked_up_by (pick-up).

    Trips used to be generated wholesale inside tasks_for_papa, so every child's activity
    landed on the father whatever the activity said. An activity naming nobody now produces
    no trip task at all rather than guessing — views.week_preparation surfaces those as
    « trajets à attribuer » instead of hiding the gap behind a wrong name.

    Ids key on the activity's pk (drive_/pickup_ + id), stable across deletions."""
    tasks = []
    for kid, name in _kid_names(settings):
        for a in activities_for(kid, day, activities, date):
            time_label = a.time_range_label()
            start = a.start_time.strftime('%Hh%M') if a.start_time else ''
            end = a.end_time.strftime('%Hh%M') if a.end_time else ''
            if a.accompanied_by == person:
                label = f"Emmener {name} à {a.label}" + (f" ({start})" if start else '')
                tasks.append(t(f'drive_{a.id}', label, phase_for_time(a.start_time)))
            if a.picked_up_by == person:
                label = f"Récupérer {name} — {a.label}" + (f" ({end})" if end else '')
                tasks.append(t(f'pickup_{a.id}', label, phase_for_time(a.end_time or a.start_time)))
    return tasks


def tasks_for_papa(day, settings, activities, date=None):
    if day == 'samedi':
        tasks = [t('famille', 'Activité en famille'), t('menage', 'Aider au ménage / rangement')]
        return tasks + _kid_activity_drive_tasks('papa', day, settings, activities, date) + _own_activity_tasks('papa', day, activities, date)
    if day == 'dimanche':
        tasks = [
            t('arabe', "Emmener les enfants au cours d'arabe", 'matin'),
            t('mahlo', "Chez Mahlo (fin d'après-midi)", info=True),
            t('lessive', 'Aider à la lessive'),
        ]
        return tasks + _kid_activity_drive_tasks('papa', day, settings, activities, date) + _own_activity_tasks('papa', day, activities, date)
    if day == 'mercredi':
        tasks = [t('journee', "Gérer la journée avec les enfants (pas d'école)", info=True)]
        if not settings.papa_travaille:
            tasks.append(t('dejeuner', 'Préparer le déjeuner pour les enfants', 'midi'))
        return tasks + _kid_activity_drive_tasks('papa', day, settings, activities, date) + _own_activity_tasks('papa', day, activities, date)
    if settings.papa_travaille:
        tasks = [t('travail', 'Travail', info=True), t('pickup', "Récupérer les enfants (16h30 — pas d'étude)", 'après-midi')]
        return tasks + _kid_activity_drive_tasks('papa', day, settings, activities, date) + _own_activity_tasks('papa', day, activities, date)
    tasks = [
        t('dejeuner', 'Préparer le déjeuner pour les enfants', 'midi'),
        t('pickup_midi', 'Aller chercher les enfants à 11h30', 'midi'),
        t('pickup', "Récupérer les enfants à 16h30 (pas d'étude)", 'après-midi'),
    ]
    return tasks + _kid_activity_drive_tasks('papa', day, settings, activities, date) + _own_activity_tasks('papa', day, activities, date)


def tasks_for(person, day, settings, activities, holiday_today=False, holiday_tomorrow=False,
              custom_tasks=None, day_mode='normal', date=None):
    """Builds a person's full task list for a day. `day_mode` (see active_day_mode) is a
    per-person override: 'vacances' is folded into holiday_today (a DayMode-marked holiday
    behaves like a Zone B holiday for that person alone), while 'absence'/'allegee' drop a
    reasonable subset of tasks afterwards (see _apply_day_mode) — a day with fewer tasks
    from either never penalizes completion/stars, since views._checkable_ids_for and
    _award_star_if_day_complete recompute the expected set the same way, dynamically."""
    if day_mode == 'vacances':
        holiday_today = True
    if person in ('fille', 'fils'):
        tasks = tasks_for_kid(person, day, settings, activities, holiday_today, holiday_tomorrow, date=date)
    elif person == 'maman':
        tasks = tasks_for_maman(day, settings, activities, date)
    elif person == 'papa':
        tasks = tasks_for_papa(day, settings, activities, date)
    else:
        tasks = []
    if custom_tasks:
        for ct in custom_tasks:
            if ct.person != person:
                continue
            # La date, quand on l'a, tranche pour les fréquences avancées ; sinon on retombe
            # sur la règle hebdomadaire, comme avant.
            if date is not None:
                if not custom_task_occurs_on(ct, date):
                    continue
            elif day not in (ct.days or []):
                continue
            tasks.append(t(f'custom_{ct.id}', ct.label, ct.period))
    return _apply_day_mode(tasks, day_mode)
