"""Filtres de mise en forme française."""

from django import template

register = template.Library()


@register.filter
def pluriel(value, suffixes='s'):
    """Accord français : seul un nombre strictement supérieur à 1 met au pluriel.

    Le filtre `pluralize` de Django suit l'anglais, où zéro est pluriel (« 0 items ») ;
    en français on écrit « 0 étape », « 0 produit ». Utilisation identique à pluralize :
    {{ n|pluriel }} ou {{ n|pluriel:"est,sont" }}.
    """
    singular, _, plural = suffixes.partition(',')
    if not plural:
        singular, plural = '', singular
    try:
        many = float(value) > 1
    except (TypeError, ValueError):
        try:
            many = len(value) > 1
        except TypeError:
            return ''
    return plural if many else singular
