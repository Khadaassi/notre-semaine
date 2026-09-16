from django import template

register = template.Library()


@register.filter
def index(sequence, position):
    """Looks up `sequence[position]` from a template, where `position` is itself a
    variable (Django's dotted `foo.bar` lookup only supports a literal int, not one held
    in another variable). Used by week.html to build the mobile per-day agenda from the
    same day-indexed `cells` lists the desktop table already renders — no new context,
    just a different read order over it."""
    try:
        return sequence[int(position)]
    except (TypeError, ValueError, IndexError):
        return None
