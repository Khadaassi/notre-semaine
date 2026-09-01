from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Recipe


class SignUpForm(UserCreationForm):
    invite_code = forms.CharField(label="Code d'invitation famille", max_length=50)

    class Meta:
        model = User
        fields = ('username', 'password1', 'password2')

    def clean_invite_code(self):
        from django.conf import settings
        code = self.cleaned_data.get('invite_code', '')
        if code != settings.FAMILY_INVITE_CODE:
            raise forms.ValidationError("Code d'invitation incorrect.")
        return code


class RecipeForm(forms.ModelForm):
    ingredients_text = forms.CharField(
        label="Ingrédients (séparés par une virgule)", required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Poulet, Riz, Citron...'})
    )

    class Meta:
        model = Recipe
        fields = ['name', 'category', 'photo']

    def save(self, commit=True):
        recipe = super().save(commit=False)
        raw = self.cleaned_data['ingredients_text']
        recipe.ingredients = [x.strip() for x in raw.split(',') if x.strip()]
        default_art = {'Viande': 'beef', 'Poisson': 'fish', 'Végétarien': 'veggie',
                        'Soupe': 'soup', 'Autre': 'egg'}
        recipe.art_key = default_art.get(recipe.category, 'egg')
        if commit:
            recipe.save()
        return recipe
