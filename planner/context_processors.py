"""Contexte partagé par toutes les pages connectées."""

from .models import FamilySettings


def reminders(request):
    """Expose les rappels dus à base.html, sur n'importe quelle page.

    Un rappel doit pouvoir arriver pendant qu'on est sur le menu ou sur les courses, pas
    seulement sur « Aujourd'hui » : le bandeau vit donc dans le gabarit de base. Le coût
    reste d'une requête (la ligne de réglages, déjà en cache de requête la plupart du
    temps) quand les rappels sont désactivés ou qu'on est dans la plage de calme.

    Silencieux pour un visiteur non connecté ou un compte sans famille — la tablette de
    cuisine, elle, a son propre gabarit et ne passe pas par ici."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    membership = getattr(request.user, 'familymembership', None)
    if membership is None:
        return {}
    from .views import _due_reminders  # import tardif : views importe déjà les modèles
    settings = FamilySettings.load(membership.family)
    return {'due_reminders': _due_reminders(membership.family, settings)}
