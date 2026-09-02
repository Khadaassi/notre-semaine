from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Family, Recipe, MEMBER_ROLE_CHOICES


class SignUpForm(UserCreationForm):
    invite_code = forms.CharField(
        label="Code d'invitation famille", max_length=50,
        widget=forms.TextInput(attrs={'placeholder': "Code d'invitation famille"})
    )
    role = forms.ChoiceField(label="Vous êtes", choices=MEMBER_ROLE_CHOICES)

    class Meta:
        model = User
        fields = ('username', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(['invite_code', 'role', 'username', 'password1', 'password2'])
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
    ingredients_text = forms.CharField(
        label="Ingrédients (séparés par une virgule)", required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Poulet, Riz, Citron...'})
    )

    class Meta:
        model = Recipe
        fields = ['name', 'category', 'photo']
        labels = {'name': 'Nom', 'category': 'Catégorie', 'photo': 'Photo'}

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
