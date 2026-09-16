import re

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Family, Recipe

# Matches a trailing "<number><unit>" at the end of an ingredient segment, e.g.
# "Poulet 500g" -> name="Poulet", qty="500", unit="g"; "Ail 2 gousses" ->
# name="Ail", qty="2", unit="gousses". A segment with no trailing number (e.g.
# "Citron") simply keeps the whole text as the name. Decimals must use '.'
# (e.g. "1.5"), never ',' — the comma is already the ingredient separator, so
# "1,5" would be split into two segments before this regex ever sees it.
_INGREDIENT_QTY_RE = re.compile(
    r'^(?P<name>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[^\d,]*)\s*$'
)


def parse_ingredients_text(raw):
    """Parses a comma-separated ingredients line such as
    "Poulet 500g, Riz 200g, Citron" into a list of {"name", "quantity",
    "unit"} dicts (Recipe.ingredients format). Quantity/unit are optional per
    ingredient. This is a plain regex/string parser — no AI/LLM involved."""
    result = []
    for segment in (raw or '').split(','):
        segment = segment.strip()
        if not segment:
            continue
        match = _INGREDIENT_QTY_RE.match(segment)
        name, quantity, unit = segment, None, ''
        if match:
            try:
                quantity = float(match.group('qty'))
                name = match.group('name').strip()
                unit = match.group('unit').strip()
            except ValueError:
                name, quantity, unit = segment, None, ''
        if name:
            result.append({'name': name, 'quantity': quantity, 'unit': unit})
    return result


class SignUpForm(UserCreationForm):
    """Invite code grants family access only — the role (parent vs. enfant) is no longer
    self-declared here; see views.signup for how it's assigned."""
    invite_code = forms.CharField(
        label="Code d'invitation famille", max_length=50,
        widget=forms.TextInput(attrs={'placeholder': "Code d'invitation famille"})
    )

    class Meta:
        model = User
        fields = ('username', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(['invite_code', 'username', 'password1', 'password2'])
        self.fields['username'].widget.attrs['placeholder'] = "Nom d'utilisateur"
        self.fields['password1'].widget.attrs['placeholder'] = "Mot de passe"
        self.fields['password2'].widget.attrs['placeholder'] = "Confirmer le mot de passe"

    def clean_invite_code(self):
        code = self.cleaned_data.get('invite_code', '')
        try:
            self.matched_family = Family.objects.get(invite_code=code)
        except Family.DoesNotExist:
            raise forms.ValidationError("Code d'invitation incorrect.")
        return code


class RecipeForm(forms.ModelForm):
    # Structured free-text field: "Poulet 500g, Riz 200g, Citron" — parsed by
    # parse_ingredients_text() above into {"name","quantity","unit"} dicts. Chosen over
    # a full line-by-line ingredient formset because it keeps the recipe form to one
    # quick line to fill in, matching how the rest of the app favors short, dense inputs
    # (see e.g. the comma-separated ingredients field it replaces); a quantity/unit is
    # optional per ingredient, so "Citron" alone still works.
    ingredients_text = forms.CharField(
        label="Ingrédients (ex : Poulet 500g, Riz 200g, Citron)", required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Poulet 500g, Riz 200g, Citron...'})
    )
    steps_text = forms.CharField(
        label="Étapes (une par ligne, facultatif)", required=False,
        widget=forms.Textarea(attrs={
            'rows': 4, 'placeholder': "Faire revenir l'oignon...\nAjouter le poulet et dorer...",
        })
    )

    class Meta:
        model = Recipe
        fields = ['name', 'category', 'photo', 'duration_minutes', 'is_favorite']
        labels = {
            'name': 'Nom', 'category': 'Catégorie', 'photo': 'Photo',
            'duration_minutes': 'Durée (minutes)', 'is_favorite': 'Recette favorite',
        }
        widgets = {
            'duration_minutes': forms.NumberInput(attrs={'min': 0, 'placeholder': '30'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(
            ['name', 'category', 'photo', 'duration_minutes', 'ingredients_text', 'steps_text', 'is_favorite']
        )

    def save(self, commit=True):
        recipe = super().save(commit=False)
        recipe.ingredients = parse_ingredients_text(self.cleaned_data['ingredients_text'])
        steps_raw = self.cleaned_data.get('steps_text', '')
        recipe.steps = [line.strip() for line in steps_raw.splitlines() if line.strip()]
        default_art = {'Viande': 'beef', 'Poisson': 'fish', 'Végétarien': 'veggie',
                        'Soupe': 'soup', 'Autre': 'egg'}
        recipe.art_key = default_art.get(recipe.category, 'egg')
        if commit:
            recipe.save()
        return recipe
