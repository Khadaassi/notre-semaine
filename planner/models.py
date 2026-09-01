from django.db import models

DAY_CHOICES = [
    ('lundi', 'Lundi'), ('mardi', 'Mardi'), ('mercredi', 'Mercredi'),
    ('jeudi', 'Jeudi'), ('vendredi', 'Vendredi'), ('samedi', 'Samedi'),
    ('dimanche', 'Dimanche'),
]
PERSON_CHOICES = [('fille', 'Fille'), ('fils', 'Fils'), ('maman', 'Maman'), ('papa', 'Papa')]
RECIPE_CATS = [('Viande', 'Viande'), ('Poisson', 'Poisson'), ('Végétarien', 'Végétarien'),
               ('Soupe', 'Soupe'), ('Autre', 'Autre')]


class FamilySettings(models.Model):
    """Singleton-style settings row (there should only ever be one)."""
    fille_name = models.CharField(max_length=50, default='Aliyah')
    fils_name = models.CharField(max_length=50, default='Zayd')
    papa_name = models.CharField(max_length=50, default='Papa')
    tt2_day = models.CharField(max_length=10, choices=DAY_CHOICES, default='vendredi')
    courses_day = models.CharField(max_length=10, choices=DAY_CHOICES, default='mercredi')
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
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Activity(models.Model):
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    label = models.CharField(max_length=100)
    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    time = models.CharField(max_length=20, blank=True, default='')

    def __str__(self):
        return f"{self.get_person_display()} — {self.label} ({self.get_day_display()})"


class TaskCompletion(models.Model):
    """One row per checkable task, per person, per calendar date."""
    person = models.CharField(max_length=10, choices=PERSON_CHOICES)
    date = models.DateField()
    task_id = models.CharField(max_length=60)
    done = models.BooleanField(default=False)

    class Meta:
        unique_together = ('person', 'date', 'task_id')

    def __str__(self):
        return f"{self.person} {self.date} {self.task_id}={self.done}"


class Recipe(models.Model):
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
    week_start = models.DateField()
    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = ('week_start', 'day')

    def __str__(self):
        return f"{self.week_start} {self.day}: {self.recipe}"


class GroceryItem(models.Model):
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=100, blank=True, default='')
    checked = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return self.name
