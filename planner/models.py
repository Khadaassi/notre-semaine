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

    class Meta:
        verbose_name = "Réglages famille"
        verbose_name_plural = "Réglages famille"

    def __str__(self):
        return "Réglages"

    @classmethod
    def load(cls, family):
        obj, _ = cls.objects.get_or_create(family=family)
        return obj


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


class Recipe(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=20, choices=RECIPE_CATS, default='Autre')
    art_key = models.CharField(max_length=20, default='egg')
    photo = models.ImageField(upload_to='recipes/', blank=True, null=True)
    ingredients = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name


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


class CustomTask(models.Model):
    """A task added from the UI, on top of the built-in routine — same shape (person,
    day, a time-of-day slot), rendered alongside the built-in tasks in 'Aujourd'hui'."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    period = models.CharField(max_length=10, choices=PHASE_CHOICES, default='matin')
    label = models.CharField(max_length=150)

    class Meta:
        ordering = ['day', 'period', 'id']

    def __str__(self):
        return f"{self.get_person_display()} — {self.label} ({self.get_day_display()})"


TASK_EXCEPTION_KIND_CHOICES = [
    ('disabled_once', 'Désactivée pour ce jour'),
    ('disabled_from', 'Désactivée à partir de cette date'),
    ('not_applicable', "Non applicable ce jour (n'affecte pas le taux de complétion)"),
]


class TaskException(models.Model):
    """An override on a generated (task_logic) or custom task, keyed by its task_id — lets
    a parent skip a task for one day, suspend it indefinitely, or mark a day where it
    doesn't apply without that counting against completion/star eligibility. See
    task_logic.split_by_exceptions for how this is applied; no management UI yet (routines-v2)."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    task_id = models.CharField(max_length=60)
    kind = models.CharField(max_length=20, choices=TASK_EXCEPTION_KIND_CHOICES)
    # For 'disabled_once' / 'not_applicable': the exact date it applies to.
    # For 'disabled_from': the date from which the task is suspended (inclusive).
    date = models.DateField()
    # Lets a 'disabled_from' suspension be explicitly reactivated (turned back on) without
    # deleting the historical row — flip to False to reactivate.
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
    ('allegee', 'Journée allégée'),
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


class GroceryItem(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=100, blank=True, default='')
    checked = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name
