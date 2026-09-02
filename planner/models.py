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
    time = models.CharField(max_length=20, blank=True, default='')

    def __str__(self):
        return f"{self.get_person_display()} — {self.label} ({self.get_day_display()})"


class TaskCompletion(models.Model):
    """One row per checkable task, per person, per calendar date."""
    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    date = models.DateField()
    task_id = models.CharField(max_length=60)
    done = models.BooleanField(default=False)

    class Meta:
        unique_together = ('family', 'person', 'date', 'task_id')

    def __str__(self):
        return f"{self.person} {self.date} {self.task_id}={self.done}"


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
