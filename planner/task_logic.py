"""
Pure functions that generate each family member's task list for a given day.
This mirrors the logic of the original single-file prototype, ported to Python.
"""
import datetime

DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
DAY_FULL = {'lundi': 'Lundi', 'mardi': 'Mardi', 'mercredi': 'Mercredi', 'jeudi': 'Jeudi',
            'vendredi': 'Vendredi', 'samedi': 'Samedi', 'dimanche': 'Dimanche'}
SCHOOL_DAYS = ['lundi', 'mardi', 'jeudi', 'vendredi']
DEEP_CLEAN_ROOMS = {'lundi': 'Salon', 'mardi': 'Salle de bain', 'mercredi': 'Chambre parents',
                     'jeudi': 'Chambre des enfants', 'vendredi': 'Entrée & couloir'}


def next_day(day):
    i = DAYS.index(day)
    return DAYS[(i + 1) % 7]


def is_school_day(day):
    return day in SCHOOL_DAYS


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


def activities_for(person, day, activities):
    return [a for a in activities if a.person == person and a.day == day]


def douche_today(kid, day, activities):
    sport = len(activities_for(kid, day, activities)) > 0
    idx = DAYS.index(day)
    alternate = idx % 2 == 0  # lundi, mercredi, vendredi
    return sport or alternate


def _own_activity_tasks(person, day, activities):
    """A person's own scheduled activities (sport, etc.) as checkable tasks — separate
    from _kid_activity_drive_tasks, which is papa driving a *kid* to theirs."""
    tasks = []
    for i, a in enumerate(activities_for(person, day, activities)):
        label = a.label + (f" ({a.time}, à confirmer)" if a.time else '')
        tasks.append(t(f'activite{i}', label, 'soir'))
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


def build_evening_school_like(kid, day, settings, activities, holiday_tomorrow=False):
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
        t('coran', 'Coran — lecture & apprentissage (~1h)', 'soir'),
    ]
    tasks += _own_activity_tasks(kid, day, activities)
    if douche_today(kid, day, activities):
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


def tasks_for_kid(kid, day, settings, activities, holiday_today=False, holiday_tomorrow=False):
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
            t('coran', 'Coran — révision légère', 'journée'),
        ]
        tasks += _weekend_rotation_tasks(kid, settings)
        tasks += _own_activity_tasks(kid, day, activities)
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
            t('coran', 'Coran — lecture', 'journée'),
        ]
        tasks += _weekend_rotation_tasks(kid, settings)
        tasks += _own_activity_tasks(kid, day, activities)
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
    return tasks + build_evening_school_like(kid, day, settings, activities, holiday_tomorrow=holiday_tomorrow)


def tasks_for_maman(day, settings, activities):
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
        tasks += _own_activity_tasks('maman', day, activities)
    elif day == 'dimanche':
        tasks = [
            t('hammam', 'Hammam'),
            t('mealprep', 'Préparation des repas de la semaine (batch cooking)'),
            t('gouters', 'Préparer les goûters sains de la semaine'),
            t('sport', 'Sport', 'matin'),
            t('repos', 'Repos'),
        ]
        tasks += _own_activity_tasks('maman', day, activities)
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
        tasks += _own_activity_tasks('maman', day, activities)
        tasks.append(t('repas', 'Préparer le repas du soir', 'soir'))
        tasks.append(t('gouter_pret', 'Goûter sain des enfants prêt', 'soir'))
        tasks.append(t('diner_famille', 'Dîner en famille', 'soir'))
        tasks.append(t('cuisine', 'Ranger la cuisine', 'soir'))
        if DEEP_CLEAN_ROOMS.get(day):
            tasks.append(t('deepclean', 'Deep clean : ' + DEEP_CLEAN_ROOMS[day], 'soir'))
    if lunchbox_demain and settings.maman_travaille:
        tasks.append(t('lunchbox_demain', 'Préparer ma lunch box pour demain', 'soir'))
    return tasks


def _kid_activity_drive_tasks(day, settings, activities):
    tasks = []
    kids = [('fille', settings.fille_name)]
    if settings.nb_enfants == 2:
        kids.append(('fils', settings.fils_name))
    for kid, name in kids:
        for i, a in enumerate(activities_for(kid, day, activities)):
            label = f"Emmener {name} à {a.label}" + (f" ({a.time}, à confirmer)" if a.time else '')
            tasks.append(t(f'drive_{kid}{i}', label, 'soir'))
    return tasks


def tasks_for_papa(day, settings, activities):
    if day == 'samedi':
        tasks = [t('famille', 'Activité en famille'), t('menage', 'Aider au ménage / rangement')]
        return tasks + _own_activity_tasks('papa', day, activities)
    if day == 'dimanche':
        tasks = [
            t('arabe', "Emmener les enfants au cours d'arabe", 'matin'),
            t('mahlo', "Chez Mahlo (fin d'après-midi)", info=True),
            t('lessive', 'Aider à la lessive'),
        ]
        return tasks + _own_activity_tasks('papa', day, activities)
    if day == 'mercredi':
        tasks = [t('journee', "Gérer la journée avec les enfants (pas d'école)", info=True)]
        if not settings.papa_travaille:
            tasks.append(t('dejeuner', 'Préparer le déjeuner pour les enfants', 'midi'))
        return tasks + _kid_activity_drive_tasks(day, settings, activities) + _own_activity_tasks('papa', day, activities)
    if settings.papa_travaille:
        tasks = [t('travail', 'Travail', info=True), t('pickup', "Récupérer les enfants (16h30 — pas d'étude)", 'après-midi')]
        return tasks + _kid_activity_drive_tasks(day, settings, activities) + _own_activity_tasks('papa', day, activities)
    tasks = [
        t('dejeuner', 'Préparer le déjeuner pour les enfants', 'midi'),
        t('pickup_midi', 'Aller chercher les enfants à 11h30', 'midi'),
        t('pickup', "Récupérer les enfants à 16h30 (pas d'étude)", 'après-midi'),
    ]
    return tasks + _kid_activity_drive_tasks(day, settings, activities) + _own_activity_tasks('papa', day, activities)


def tasks_for(person, day, settings, activities, holiday_today=False, holiday_tomorrow=False,
              custom_tasks=None):
    if person in ('fille', 'fils'):
        tasks = tasks_for_kid(person, day, settings, activities, holiday_today, holiday_tomorrow)
    elif person == 'maman':
        tasks = tasks_for_maman(day, settings, activities)
    elif person == 'papa':
        tasks = tasks_for_papa(day, settings, activities)
    else:
        tasks = []
    if custom_tasks:
        for ct in custom_tasks:
            if ct.person == person and ct.day == day:
                tasks.append(t(f'custom_{ct.id}', ct.label, ct.period))
    return tasks
