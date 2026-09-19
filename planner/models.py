import datetime
import secrets
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import models

DAY_CHOICES = [
    ('lundi', 'Lundi'), ('mardi', 'Mardi'), ('mercredi', 'Mercredi'),
    ('jeudi', 'Jeudi'), ('vendredi', 'Vendredi'), ('samedi', 'Samedi'),
    ('dimanche', 'Dimanche'),
]
PERSON_CHOICES = [('fille', 'Fille'), ('fils', 'Fils'), ('maman', 'Maman'), ('papa', 'Papa')]
RECIPE_CATS = [('Viande', 'Viande'), ('Poisson', 'Poisson'), ('Végétarien', 'Végétarien'),
               ('Soupe', 'Soupe'), ('Autre', 'Autre')]


class Family(models.Model):
    """A household using the app — everything else is scoped to one of these, so
    several families can share one deployment without seeing each other's data."""
    name = models.CharField(max_length=100, blank=True, default='')
    invite_code = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or self.invite_code


MEMBER_ROLE_CHOICES = [('maman', 'Maman'), ('papa', 'Papa'), ('enfants', 'Enfants')]
PARENT_ROLES = ('maman', 'papa')


class FamilyMembership(models.Model):
    """Links a login (User) to the one Family whose data it can see, and the role
    (parent vs. shared kids' account) that governs what it's allowed to change."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='members')
    role = models.CharField(max_length=10, choices=MEMBER_ROLE_CHOICES, default='enfants')

    def __str__(self):
        return f"{self.user} → {self.family} ({self.role})"


NB_ENFANTS_CHOICES = [(1, '1 enfant'), (2, '2 enfants')]


class FamilySettings(models.Model):
    """One settings row per family."""
    family = models.OneToOneField(Family, on_delete=models.CASCADE)
    maman_name = models.CharField(max_length=50, default='Maman')
    fille_name = models.CharField(max_length=50, default='Aliyah')
    fils_name = models.CharField(max_length=50, default='Zayd')
    papa_name = models.CharField(max_length=50, default='Papa')
    nb_enfants = models.IntegerField(choices=NB_ENFANTS_CHOICES, default=2)
    tt2_day = models.CharField(max_length=10, choices=DAY_CHOICES, default='vendredi')
    courses_day = models.CharField(max_length=10, choices=DAY_CHOICES, default='mercredi')
    maman_travaille = models.BooleanField(default=True)
    papa_travaille = models.BooleanField(default=False)
    rotation_table = models.CharField(max_length=10, choices=PERSON_CHOICES, default='fille')
    rotation_lave_vaisselle = models.CharField(max_length=10, choices=PERSON_CHOICES, default='fils')
    week_note = models.TextField(blank=True, default='')
    # Reward settings (see views._award_star_if_day_complete / views.stars_view): configurable
    # per family instead of the old global STAR_MILESTONE constant.
    star_milestone = models.PositiveIntegerField(
        default=15, help_text="Nombre de journées entièrement cochées avant la surprise."
    )
    star_reward_text = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Texte affiché dans le popup de surprise (facultatif — sinon message générique)."
    )
    # Long, unguessable slug for the unauthenticated read-only kitchen tablet display
    # (see views.tablet_view) — generated once in `load()`, regenerable from settings.
    tablet_token = models.CharField(max_length=64, blank=True, default='')
    # Rappels de routine (voir RoutineReminder). Interrupteur global de la famille + plage
    # de calme pendant laquelle aucun rappel ne s'affiche, même si son heure est passée.
    # Les heures sont interprétées en heure locale Django (Europe/Paris), jamais en heure
    # système du serveur — voir views._now().
    reminders_enabled = models.BooleanField(
        default=True, help_text="Afficher les rappels de routine des enfants dans l'application."
    )
    quiet_start = models.TimeField(
        default=datetime.time(20, 30), help_text="Début de la plage sans rappel (le soir)."
    )
    quiet_end = models.TimeField(
        default=datetime.time(7, 0), help_text="Fin de la plage sans rappel (le matin)."
    )

    def in_quiet_hours(self, value):
        """Plage de calme, éventuellement à cheval sur minuit (20 h 30 -> 7 h).

        Si les deux bornes sont égales, la plage est vide : on considère qu'il n'y a pas
        d'heures calmes, plutôt qu'un silence permanent qui rendrait les rappels invisibles
        sans que personne comprenne pourquoi."""
        start, end = self.quiet_start, self.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= value < end
        return value >= start or value < end

    class Meta:
        verbose_name = "Réglages famille"
        verbose_name_plural = "Réglages famille"

    def __str__(self):
        return "Réglages"

    @classmethod
    def load(cls, family):
        obj, _ = cls.objects.get_or_create(family=family)
        if not obj.tablet_token:
            obj.tablet_token = secrets.token_urlsafe(24)
            obj.save(update_fields=['tablet_token'])
        return obj

    def regenerate_tablet_token(self):
        self.tablet_token = secrets.token_urlsafe(24)
        self.save(update_fields=['tablet_token'])


class Activity(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    label = models.CharField(max_length=100)
    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    # Optional: a one-off occurrence on a precise calendar date, on top of the recurring
    # weekly `day` above (e.g. a single rescheduled practice) — not yet surfaced in the UI.
    specific_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    accompanied_by = models.CharField(max_length=10, choices=PERSON_CHOICES, blank=True, default='')
    picked_up_by = models.CharField(max_length=10, choices=PERSON_CHOICES, blank=True, default='')
    location = models.CharField(max_length=150, blank=True, default='')
    items_to_bring = models.TextField(blank=True, default='')

    def __str__(self):
        return f"{self.get_person_display()} — {self.label} ({self.get_day_display()})"

    def time_range_label(self):
        """Formats start/end time for display — replaces the old freeform `time` text field."""
        if self.start_time and self.end_time:
            return f"{self.start_time.strftime('%Hh%M')}–{self.end_time.strftime('%Hh%M')}"
        if self.start_time:
            return self.start_time.strftime('%Hh%M')
        return ''


class TaskCompletion(models.Model):
    """One row per checkable task, per person, per calendar date."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    date = models.DateField()
    task_id = models.CharField(max_length=60)
    done = models.BooleanField(default=False)
    seconds_spent = models.PositiveIntegerField(default=0)
    timer_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('family', 'person', 'date', 'task_id')

    def __str__(self):
        return f"{self.person} {self.date} {self.task_id}={self.done}"


class TaskOrder(models.Model):
    """Per-family, per-person custom display order for a task_id — lets whoever may edit a
    card's tasks (see checkable_by_viewer in views.today) drag the routine into their own
    order without touching the generated task list itself."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    task_id = models.CharField(max_length=60)
    order = models.IntegerField(default=0)

    class Meta:
        unique_together = ('family', 'person', 'task_id')

    def __str__(self):
        return f"{self.person} {self.task_id} → {self.order}"


class StarAward(models.Model):
    """One row per day a kid fully completed every checkable task — the audit trail behind
    KidStars.total, and what stops the same day from awarding a star twice."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    date = models.DateField()

    class Meta:
        unique_together = ('family', 'person', 'date')

    def __str__(self):
        return f"{self.person} {self.date}"


class KidStars(models.Model):
    """Cumulative reward-star count per kid. Individual stars aren't shown to the kid —
    only the milestone (every FamilySettings.star_milestone, see views.py) surfaces as a surprise."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    total = models.PositiveIntegerField(default=0)
    milestones_shown = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('family', 'person')
        verbose_name = "Étoiles"
        verbose_name_plural = "Étoiles"

    def __str__(self):
        return f"{self.person}: {self.total} étoile(s)"


def format_quantity(quantity):
    """Renders a Decimal/int/float/None quantity as a compact string: trailing
    zeros are dropped ('500.00' -> '500', '1.50' -> '1.5') but scientific
    notation is never used. Returns '' for None/''/unparseable input."""
    if quantity in (None, ''):
        return ''
    if not isinstance(quantity, Decimal):
        try:
            quantity = Decimal(str(quantity))
        except InvalidOperation:
            return ''
    text = format(quantity, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text


class Recipe(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=20, choices=RECIPE_CATS, default='Autre')
    art_key = models.CharField(max_length=20, default='egg')
    photo = models.ImageField(upload_to='recipes/', blank=True, null=True)
    # List of {"name": str, "quantity": number|None, "unit": str} dicts (Lot 4). Older rows
    # created before Lot 4 stored a plain list of name strings — migration 0016 converts
    # existing DB rows, but normalize_ingredients()/ingredients_list() below stay tolerant
    # of the old shape too, in case a string-list row slips through (fixture, admin edit, …).
    ingredients = models.JSONField(default=list, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    # List of step description strings, in order.
    steps = models.JSONField(default=list, blank=True)
    is_favorite = models.BooleanField(default=False)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name

    @staticmethod
    def normalize_ingredients(raw):
        """Returns `raw` (Recipe.ingredients) as a list of
        {"name", "quantity", "unit"} dicts, tolerating the pre-Lot-4 format
        (a plain list of ingredient-name strings)."""
        result = []
        for entry in raw or []:
            if isinstance(entry, dict):
                name = (entry.get('name') or '').strip()
                if not name:
                    continue
                result.append({
                    'name': name,
                    'quantity': entry.get('quantity'),
                    'unit': (entry.get('unit') or '').strip(),
                })
            elif isinstance(entry, str):
                name = entry.strip()
                if name:
                    result.append({'name': name, 'quantity': None, 'unit': ''})
        return result

    def ingredients_list(self):
        """This recipe's ingredients, normalized (see normalize_ingredients)."""
        return Recipe.normalize_ingredients(self.ingredients)

    def ingredients_display(self):
        """Human-readable 'Name — qty unit' strings, for tag-row templates."""
        out = []
        for ing in self.ingredients_list():
            label = ing['name']
            qty_text = format_quantity(ing['quantity'])
            if qty_text and ing['unit']:
                label += f" — {qty_text} {ing['unit']}"
            elif qty_text:
                label += f" — {qty_text}"
            elif ing['unit']:
                label += f" — {ing['unit']}"
            out.append(label)
        return out

    @classmethod
    def aggregate_ingredients(cls, recipes):
        """Merges the ingredient lists of several recipes into one shopping
        list: entries sharing the same name (case-insensitive) and unit have
        their quantities summed. If ANY contributing occurrence of a given
        name+unit has no known quantity, the merged quantity is None (unknown)
        rather than silently treating the missing value as zero — so the
        result never claims a precision it doesn't have.

        Returns a list of {"name", "unit", "quantity"} dicts (quantity is a
        Decimal or None), in first-seen order.
        """
        groups = {}
        order = []
        for recipe in recipes:
            for ing in cls.normalize_ingredients(recipe.ingredients):
                unit = ing['unit']
                key = (ing['name'].lower(), unit.lower())
                if key not in groups:
                    groups[key] = {
                        'name': ing['name'], 'unit': unit,
                        'total': Decimal('0'), 'has_qty': False, 'has_unknown': False,
                    }
                    order.append(key)
                group = groups[key]
                qty = ing['quantity']
                if qty in (None, ''):
                    group['has_unknown'] = True
                    continue
                try:
                    group['total'] += Decimal(str(qty))
                    group['has_qty'] = True
                except (InvalidOperation, ValueError, TypeError):
                    group['has_unknown'] = True
        result = []
        for key in order:
            g = groups[key]
            quantity = g['total'] if (g['has_qty'] and not g['has_unknown']) else None
            result.append({'name': g['name'], 'unit': g['unit'], 'quantity': quantity})
        return result


class WeeklyMenuEntry(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    week_start = models.DateField()
    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = ('family', 'week_start', 'day')

    def __str__(self):
        return f"{self.week_start} {self.day}: {self.recipe}"


PHASE_CHOICES = [('matin', 'Matin'), ('journee', 'Journée'), ('soir', 'Soir')]


DAY_LABELS = dict(DAY_CHOICES)


class CustomTask(models.Model):
    """A task added from the UI, on top of the built-in routine — same shape (person,
    a time-of-day slot), rendered alongside the built-in tasks in 'Aujourd'hui'. `days` is
    the list of weekday keys it recurs on (its "frequency") — replaces the old single-day
    `day` field (see the 0015-0017 migrations for the day -> days conversion)."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    days = models.JSONField(default=list, blank=True)
    period = models.CharField(max_length=10, choices=PHASE_CHOICES, default='matin')
    label = models.CharField(max_length=150)

    class Meta:
        ordering = ['period', 'id']

    def __str__(self):
        return f"{self.get_person_display()} — {self.label} ({self.days_display()})"

    def days_display(self):
        return ', '.join(DAY_LABELS.get(d, d) for d in self.days)


TASK_EXCEPTION_KIND_CHOICES = [
    ('disabled_once', 'Désactivée pour ce jour'),
    ('disabled_from', 'Désactivée à partir de cette date'),
    ('not_applicable', "Non applicable ce jour (n'affecte pas le taux de complétion)"),
    ('reassigned', 'Réattribuée à un autre membre pour ce jour'),
]


class TaskException(models.Model):
    """An override on a generated (task_logic) or custom task, keyed by its task_id — lets
    a parent skip a task for one day, suspend it indefinitely, mark a day where it doesn't
    apply without that counting against completion/star eligibility, or hand it off to
    another family member for one day. See task_logic.split_by_exceptions for how
    disabled/not_applicable are applied, and views._reassignment_maps for 'reassigned'."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    task_id = models.CharField(max_length=60)
    kind = models.CharField(max_length=20, choices=TASK_EXCEPTION_KIND_CHOICES)
    # For 'disabled_once' / 'not_applicable' / 'reassigned': the exact date it applies to.
    # For 'disabled_from': the date from which the task is suspended (inclusive).
    date = models.DateField()
    # Only set (and only meaningful) for kind='reassigned': who the task is handed to for
    # that date — the task disappears from `person`'s list and appears in this person's.
    reassigned_to = models.CharField(max_length=10, choices=PERSON_CHOICES, blank=True, default='')
    # Lets a 'disabled_from' suspension (or any other exception) be explicitly reactivated
    # (turned back on) without deleting the historical row — flip to False to reactivate.
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['family', 'person', 'task_id'])]

    def __str__(self):
        return f"{self.person} {self.task_id} {self.kind} {self.date}"


DAY_MODE_CHOICES = [
    ('normal', 'École normale'),
    ('vacances', 'Vacances'),
    ('absence', 'Absence'),
    ('allegee', 'Malade / Journée allégée'),
]


class DayMode(models.Model):
    """Marks a person's day with a mode other than 'normal' school routine — will drive
    which tasks get generated in the routines-v2 UI. See task_logic.active_day_mode for the
    lookup helper; querying with no row present means 'normal'."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    date = models.DateField()
    mode = models.CharField(max_length=20, choices=DAY_MODE_CHOICES, default='normal')

    class Meta:
        unique_together = ('family', 'person', 'date')

    def __str__(self):
        return f"{self.person} {self.date}: {self.mode}"


class HelpRequest(models.Model):
    """« Besoin d'aide » levé par un enfant pendant la routine guidée, pour une tâche et un
    jour donnés.

    Volontairement à part de la progression : celle-ci reste entièrement portée par
    TaskCompletion (une seule source, partagée avec la checklist). Ce drapeau ne fait
    qu'afficher un état visible dans l'espace de l'enfant concerné — il ne bloque pas
    l'autre enfant et ne déclenche aucune notification externe. Résolu (active=False) dès
    que l'aide est arrivée ou que la tâche est validée."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    date = models.DateField()
    task_id = models.CharField(max_length=60)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('family', 'person', 'date', 'task_id')
        indexes = [models.Index(fields=['family', 'date', 'active'])]

    def __str__(self):
        return f"{self.person} {self.date} {self.task_id} aide={'oui' if self.active else 'non'}"


class GroceryItem(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=100, blank=True, default='')
    checked = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)
    # True = "we already have this at home" — excludes it from the "à acheter" view
    # without deleting it from the list (Lot 4, point 2).
    already_home = models.BooleanField(default=False)
    quantity = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True, default='')
    # Lundi de la semaine dont ce produit vient (transfert menu → courses). NULL = produit
    # habituel ou ajout manuel, qui n'appartient à aucune semaine et n'est donc jamais
    # remplacé ni supprimé quand on régénère les courses d'une autre semaine.
    week_start = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name

    def is_week_item(self):
        return self.week_start is not None

    def quantity_display(self):
        text = format_quantity(self.quantity)
        if text and self.unit:
            return f"{text} {self.unit}"
        return text or self.unit


REMINDER_PHASE_CHOICES = PHASE_CHOICES


class RoutineReminder(models.Model):
    """Un rappel de routine pour UN enfant, à une heure et sur des jours choisis.

    Rappel *dans l'application*, et rien d'autre : il s'affiche sur « Aujourd'hui » et sur
    la routine guidée quand l'heure locale est passée et que la routine visée n'est pas
    finie. Aucun SMS, aucune notification push, aucun service externe — c'est une limite
    assumée, écrite noir sur blanc dans les réglages pour que personne ne compte dessus
    pour être prévenu quand l'app est fermée.

    Réservé aux enfants (`person` dans fille/fils) : les parents organisent leur journée
    eux-mêmes, et l'écran des enfants est le seul où un rappel a un sens pédagogique.

    `acked_on` porte la mise en sourdine du jour (« Plus tard ») : une seule date sur la
    ligne, donc pas de table d'accusés qui grossirait à chaque jour et à chaque rappel.
    """
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    phase = models.CharField(max_length=10, choices=REMINDER_PHASE_CHOICES, default='matin')
    at_time = models.TimeField()
    days = models.JSONField(default=list, blank=True)
    label = models.CharField(max_length=120, blank=True, default='')
    active = models.BooleanField(default=True)
    # Jour où le rappel a été mis en sourdine depuis l'écran ; il repart tout seul le lendemain.
    acked_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['at_time', 'person', 'id']
        indexes = [models.Index(fields=['family', 'active'])]

    def __str__(self):
        return f"{self.person} {self.phase} {self.at_time:%H:%M} ({self.days_display()})"

    def days_display(self):
        return ', '.join(DAY_LABELS.get(d, d) for d in self.days)

    def phase_display(self):
        return dict(REMINDER_PHASE_CHOICES).get(self.phase, self.phase)

    def default_label(self, person_name):
        return f"Routine du {self.phase_display().lower()} de {person_name}"

    def occurs_on_day(self, day):
        return day in (self.days or [])
