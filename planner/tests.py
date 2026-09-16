import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException, WeeklyMenuEntry, Recipe, Activity,
)
from .task_logic import is_zone_b_holiday, ZONE_B_HOLIDAYS, find_schedule_conflicts
from .views import _checkable_ids_for, _award_star_if_day_complete, _real_date_for_day, _monday_of


class ParentRequiredViewsTests(TestCase):
    """An 'enfants' account must be turned away from parent-only views (settings, member
    promotion, member removal); a parent account must be let through."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='TESTCODE1')
        self.parent_user = User.objects.create_user('parent1', password='pass12345')
        FamilyMembership.objects.create(user=self.parent_user, family=self.family, role='maman')
        self.child_user = User.objects.create_user('child1', password='pass12345')
        self.child_membership = FamilyMembership.objects.create(
            user=self.child_user, family=self.family, role='enfants'
        )

    def test_child_cannot_access_settings(self):
        self.client.force_login(self.child_user)
        resp = self.client.get(reverse('settings'))
        self.assertEqual(resp.status_code, 403)

    def test_parent_can_access_settings(self):
        self.client.force_login(self.parent_user)
        resp = self.client.get(reverse('settings'))
        self.assertEqual(resp.status_code, 200)

    def test_child_cannot_promote_member(self):
        self.client.force_login(self.child_user)
        resp = self.client.post(
            reverse('promote_member', args=[self.child_membership.id]), {'role': 'maman'}
        )
        self.assertEqual(resp.status_code, 403)
        self.child_membership.refresh_from_db()
        self.assertEqual(self.child_membership.role, 'enfants')

    def test_parent_can_promote_member(self):
        self.client.force_login(self.parent_user)
        resp = self.client.post(
            reverse('promote_member', args=[self.child_membership.id]), {'role': 'papa'}
        )
        self.assertEqual(resp.status_code, 302)
        self.child_membership.refresh_from_db()
        self.assertEqual(self.child_membership.role, 'papa')

    def test_child_cannot_remove_member(self):
        parent_membership = self.parent_user.familymembership
        self.client.force_login(self.child_user)
        resp = self.client.post(reverse('remove_member', args=[parent_membership.id]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(FamilyMembership.objects.filter(pk=parent_membership.id).exists())


class ZoneBHolidayTests(TestCase):
    def test_every_holiday_period_is_detected(self):
        for label, start, end in ZONE_B_HOLIDAYS:
            with self.subTest(period=label):
                self.assertTrue(is_zone_b_holiday(start))
                self.assertTrue(is_zone_b_holiday(end))
                self.assertTrue(is_zone_b_holiday(start + datetime.timedelta(days=1)))

    def test_date_outside_any_holiday_is_not_flagged(self):
        self.assertFalse(is_zone_b_holiday(datetime.date(2026, 9, 15)))


class StarAwardTests(TestCase):
    """Covers _award_star_if_day_complete: one star per fully-completed day, no double
    award on re-checking, and TaskException(kind='not_applicable') excluded from the
    completion calculation entirely (see task_logic.split_by_exceptions)."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='STARFAM1')
        FamilySettings.load(self.family)
        self.day = 'lundi'
        self.person = 'fille'
        self.real_date = _real_date_for_day(self.day)

    def _complete(self, task_ids):
        for task_id in task_ids:
            TaskCompletion.objects.update_or_create(
                family=self.family, person=self.person, date=self.real_date, task_id=task_id,
                defaults={'done': True},
            )

    def test_completing_every_task_awards_one_star(self):
        checkable_ids = _checkable_ids_for(self.person, self.day, self.family)
        self.assertTrue(checkable_ids)
        self._complete(checkable_ids)

        _award_star_if_day_complete(self.family, self.person, self.day, self.real_date)

        self.assertEqual(
            StarAward.objects.filter(family=self.family, person=self.person, date=self.real_date).count(), 1
        )
        self.assertEqual(KidStars.objects.get(family=self.family, person=self.person).total, 1)

    def test_rechecking_the_same_day_does_not_award_twice(self):
        checkable_ids = _checkable_ids_for(self.person, self.day, self.family)
        self._complete(checkable_ids)

        _award_star_if_day_complete(self.family, self.person, self.day, self.real_date)
        _award_star_if_day_complete(self.family, self.person, self.day, self.real_date)

        self.assertEqual(
            StarAward.objects.filter(family=self.family, person=self.person, date=self.real_date).count(), 1
        )
        self.assertEqual(KidStars.objects.get(family=self.family, person=self.person).total, 1)

    def test_not_applicable_task_does_not_block_star(self):
        all_ids = _checkable_ids_for(self.person, self.day, self.family)
        excluded_id = sorted(all_ids)[0]
        TaskException.objects.create(
            family=self.family, person=self.person, task_id=excluded_id,
            kind='not_applicable', date=self.real_date,
        )

        remaining_ids = _checkable_ids_for(self.person, self.day, self.family)
        self.assertNotIn(excluded_id, remaining_ids)

        self._complete(remaining_ids)
        _award_star_if_day_complete(self.family, self.person, self.day, self.real_date)

        self.assertTrue(
            StarAward.objects.filter(family=self.family, person=self.person, date=self.real_date).exists()
        )


class SignupRoleTests(TestCase):
    """The invite code alone must never grant a parent role: only the very first person to
    join a brand-new family is auto-promoted (otherwise no one could ever promote anyone)."""

    def setUp(self):
        self.family = Family.objects.create(name='NewFam', invite_code='FRESHCODE1')

    def _signup(self, username):
        return self.client.post(reverse('signup'), {
            'invite_code': self.family.invite_code,
            'username': username,
            'password1': 'SuperSecret123!',
            'password2': 'SuperSecret123!',
        })

    def test_first_member_becomes_parent(self):
        resp = self._signup('firstuser')
        self.assertEqual(resp.status_code, 302)
        membership = FamilyMembership.objects.get(user__username='firstuser')
        self.assertIn(membership.role, PARENT_ROLES)

    def test_second_member_stays_child_until_promoted(self):
        self._signup('firstuser')
        self.client.logout()
        resp = self._signup('seconduser')
        self.assertEqual(resp.status_code, 302)
        membership = FamilyMembership.objects.get(user__username='seconduser')
        self.assertEqual(membership.role, 'enfants')


class WeekNavigationTests(TestCase):
    """week_view defaults to the current calendar week when ?week= is absent (unchanged
    behavior), and navigates to whichever Monday ?week= points at otherwise — snapping any
    non-Monday date to its Monday, and falling back to the current week on anything that
    doesn't parse, rather than erroring."""

    def setUp(self):
        self.family = Family.objects.create(name='NavFam', invite_code='NAVCODE1')
        FamilySettings.load(self.family)
        self.user = User.objects.create_user('navuser', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)

    def test_default_week_is_current_week(self):
        resp = self.client.get(reverse('week'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['week_start'], _monday_of(datetime.date.today()))
        self.assertTrue(resp.context['is_current_week'])

    def test_week_param_navigates_to_requested_monday(self):
        target_monday = _monday_of(datetime.date.today()) + datetime.timedelta(days=14)
        resp = self.client.get(reverse('week'), {'week': target_monday.isoformat()})
        self.assertEqual(resp.context['week_start'], target_monday)
        self.assertFalse(resp.context['is_current_week'])

    def test_non_monday_week_param_snaps_to_its_monday(self):
        wednesday_next_week = _monday_of(datetime.date.today()) + datetime.timedelta(days=9)
        resp = self.client.get(reverse('week'), {'week': wednesday_next_week.isoformat()})
        self.assertEqual(resp.context['week_start'], _monday_of(wednesday_next_week))

    def test_invalid_week_param_falls_back_to_current_week(self):
        resp = self.client.get(reverse('week'), {'week': 'not-a-date'})
        self.assertEqual(resp.context['week_start'], _monday_of(datetime.date.today()))

    def test_prev_next_links_point_to_adjacent_mondays(self):
        resp = self.client.get(reverse('week'))
        prev_iso = resp.context['prev_week'].isoformat()
        next_iso = resp.context['next_week'].isoformat()
        self.assertContains(resp, f'?week={prev_iso}')
        self.assertContains(resp, f'?week={next_iso}')


class WeekDuplicationTests(TestCase):
    """duplicate_week copies the displayed week's WeeklyMenuEntry rows onto the following
    week. Scope decided in views.duplicate_week: only the menu plan is duplicated — a day
    already planned in the target week is left untouched (never silently overwritten), and
    empty (no-recipe) slots aren't copied either."""

    def setUp(self):
        self.family = Family.objects.create(name='DupFam', invite_code='DUPCODE1')
        FamilySettings.load(self.family)
        self.user = User.objects.create_user('dupuser', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)
        self.source_week = _monday_of(datetime.date.today())
        self.target_week = self.source_week + datetime.timedelta(days=7)
        self.recipe = Recipe.objects.create(family=self.family, name='Poulet rôti')
        WeeklyMenuEntry.objects.create(
            family=self.family, week_start=self.source_week, day='lundi', recipe=self.recipe
        )
        WeeklyMenuEntry.objects.create(
            family=self.family, week_start=self.source_week, day='mardi', recipe=None
        )

    def test_duplicate_copies_recipe_entries_to_next_week(self):
        self.client.post(reverse('duplicate_week'), {'week': self.source_week.isoformat()})
        entry = WeeklyMenuEntry.objects.get(family=self.family, week_start=self.target_week, day='lundi')
        self.assertEqual(entry.recipe, self.recipe)

    def test_duplicate_skips_empty_recipe_slots(self):
        self.client.post(reverse('duplicate_week'), {'week': self.source_week.isoformat()})
        self.assertFalse(
            WeeklyMenuEntry.objects.filter(family=self.family, week_start=self.target_week, day='mardi').exists()
        )

    def test_duplicate_does_not_overwrite_existing_target_entry(self):
        other_recipe = Recipe.objects.create(family=self.family, name='Soupe')
        WeeklyMenuEntry.objects.create(
            family=self.family, week_start=self.target_week, day='lundi', recipe=other_recipe
        )
        self.client.post(reverse('duplicate_week'), {'week': self.source_week.isoformat()})
        entry = WeeklyMenuEntry.objects.get(family=self.family, week_start=self.target_week, day='lundi')
        self.assertEqual(entry.recipe, other_recipe)

    def test_duplicate_redirects_to_next_week(self):
        resp = self.client.post(reverse('duplicate_week'), {'week': self.source_week.isoformat()})
        self.assertRedirects(resp, f"{reverse('week')}?week={self.target_week.isoformat()}")


class WeekNoteDisplayTests(TestCase):
    """FamilySettings.week_note was saved from the settings form but never rendered
    anywhere — now shown on both 'Aujourd'hui' and 'Organisation' when non-empty."""

    def setUp(self):
        self.family = Family.objects.create(name='NoteFam', invite_code='NOTECODE1')
        self.settings = FamilySettings.load(self.family)
        self.user = User.objects.create_user('noteuser', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)

    def test_week_note_shown_on_week_view_when_set(self):
        self.settings.week_note = 'Mamie vient dîner mercredi'
        self.settings.save()
        resp = self.client.get(reverse('week'))
        self.assertContains(resp, 'Mamie vient dîner mercredi')

    def test_week_note_shown_on_today_view_when_set(self):
        self.settings.week_note = 'Anniversaire de Papa vendredi'
        self.settings.save()
        resp = self.client.get(reverse('today'))
        self.assertContains(resp, 'Anniversaire de Papa vendredi')

    def test_empty_week_note_renders_no_note_block(self):
        resp = self.client.get(reverse('week'))
        self.assertNotContains(resp, 'weeknote-label')


class ScheduleConflictTests(TestCase):
    """find_schedule_conflicts flags overlapping time ranges for the same person, or for
    two different people who'd need the same escort (accompanied_by) at once — a display-
    only signal, no automatic resolution. See task_logic.find_schedule_conflicts."""

    def _activity(self, pk, person, start, end, accompanied_by=''):
        a = Activity(person=person, label='x', day='lundi', start_time=start, end_time=end,
                      accompanied_by=accompanied_by)
        a.id = pk
        return a

    def test_same_person_overlap_is_flagged(self):
        a = self._activity(1, 'fille', datetime.time(17, 0), datetime.time(18, 0))
        b = self._activity(2, 'fille', datetime.time(17, 30), datetime.time(18, 30))
        self.assertEqual(find_schedule_conflicts([a, b]), {1, 2})

    def test_same_escort_overlap_is_flagged(self):
        a = self._activity(1, 'fille', datetime.time(17, 0), datetime.time(18, 0), accompanied_by='papa')
        b = self._activity(2, 'fils', datetime.time(17, 30), datetime.time(18, 30), accompanied_by='papa')
        self.assertEqual(find_schedule_conflicts([a, b]), {1, 2})

    def test_non_overlapping_times_are_not_flagged(self):
        a = self._activity(1, 'fille', datetime.time(17, 0), datetime.time(18, 0))
        b = self._activity(2, 'fille', datetime.time(18, 0), datetime.time(19, 0))
        self.assertEqual(find_schedule_conflicts([a, b]), set())

    def test_different_people_different_escorts_not_flagged(self):
        a = self._activity(1, 'fille', datetime.time(17, 0), datetime.time(18, 0), accompanied_by='papa')
        b = self._activity(2, 'fils', datetime.time(17, 30), datetime.time(18, 30), accompanied_by='maman')
        self.assertEqual(find_schedule_conflicts([a, b]), set())

    def test_missing_times_are_skipped_without_error(self):
        a = self._activity(1, 'fille', None, None)
        b = self._activity(2, 'fille', datetime.time(17, 0), datetime.time(18, 0))
        self.assertEqual(find_schedule_conflicts([a, b]), set())


class WeekViewActivityDisplayTests(TestCase):
    """Covers the two Activity-related additions to week.html: a `specific_date` one-off
    shows only on the exact date it falls on (never on its recurring `day` slot too), and
    two overlapping activities render the 'activity-overlap' visual flag."""

    def setUp(self):
        self.family = Family.objects.create(name='ActFam', invite_code='ACTCODE1')
        FamilySettings.load(self.family)
        self.user = User.objects.create_user('actuser', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)
        self.week_start = _monday_of(datetime.date.today())

    def test_specific_date_activity_shows_only_on_its_date(self):
        one_off_date = self.week_start + datetime.timedelta(days=3)  # jeudi
        Activity.objects.create(
            family=self.family, person='fille', label='Spectacle de danse', day='lundi',
            specific_date=one_off_date,
        )
        resp = self.client.get(reverse('week'), {'week': self.week_start.isoformat()})
        self.assertContains(resp, 'Spectacle de danse')
        # Rendered once for the desktop table cell and once for the mobile agenda — not a
        # third time under the (unrelated) 'lundi' slot from its `day` field.
        self.assertEqual(resp.content.decode().count('Spectacle de danse'), 2)

    def test_overlapping_activities_flagged_in_output(self):
        Activity.objects.create(
            family=self.family, person='papa', label='Foot', day='lundi',
            start_time=datetime.time(17, 0), end_time=datetime.time(18, 0), accompanied_by='papa',
        )
        Activity.objects.create(
            family=self.family, person='fils', label='Judo', day='lundi',
            start_time=datetime.time(17, 30), end_time=datetime.time(18, 30), accompanied_by='papa',
        )
        resp = self.client.get(reverse('week'), {'week': self.week_start.isoformat()})
        self.assertContains(resp, 'activity-overlap')

    def test_non_overlapping_activities_not_flagged(self):
        Activity.objects.create(
            family=self.family, person='papa', label='Foot', day='lundi',
            start_time=datetime.time(17, 0), end_time=datetime.time(18, 0),
        )
        resp = self.client.get(reverse('week'), {'week': self.week_start.isoformat()})
        self.assertNotContains(resp, 'activity-overlap')
