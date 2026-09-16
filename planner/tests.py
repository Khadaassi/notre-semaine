import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException, Activity, Recipe, WeeklyMenuEntry,
)
from .task_logic import is_zone_b_holiday, ZONE_B_HOLIDAYS, DAYS, tasks_for
from .views import _checkable_ids_for, _award_star_if_day_complete, _real_date_for_day


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


class KidPersonFieldTests(TestCase):
    def test_default_is_blank(self):
        family = Family.objects.create(name='F', invite_code='FIELDT01')
        user = User.objects.create_user('fielduser', password='pass12345')
        membership = FamilyMembership.objects.create(user=user, family=family, role='enfants')
        self.assertEqual(membership.kid_person, '')


class KidPersonPermissionTests(TestCase):
    """Security-sensitive: an 'enfants' account must only ever be able to see/check/time/
    reorder the one kid (kid_person) a parent assigned it to — never a sibling's tasks, and
    (until it's assigned) nothing at all rather than defaulting to full access or crashing.
    This is the bug fix at the heart of Lot 1 point 6."""

    def setUp(self):
        self.family = Family.objects.create(name='KidPerm', invite_code='KIDPERM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('kpparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.fille_user = User.objects.create_user('kpfille', password='pass12345')
        self.fille_membership = FamilyMembership.objects.create(
            user=self.fille_user, family=self.family, role='enfants', kid_person='fille'
        )
        self.unassigned_user = User.objects.create_user('kpunassigned', password='pass12345')
        FamilyMembership.objects.create(user=self.unassigned_user, family=self.family, role='enfants')
        self.day = 'lundi'

    def _toggle(self, user, person, task_id='reveil'):
        self.client.force_login(user)
        return self.client.post(reverse('toggle_task'), {
            'person': person, 'task_id': task_id, 'day': self.day, 'done': '1',
        })

    def test_assigned_kid_can_toggle_own_task(self):
        resp = self._toggle(self.fille_user, 'fille')
        self.assertEqual(resp.status_code, 200)

    def test_assigned_kid_cannot_toggle_sibling_task(self):
        resp = self._toggle(self.fille_user, 'fils')
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            TaskCompletion.objects.filter(family=self.family, person='fils', task_id='reveil').exists()
        )

    def test_unassigned_kid_cannot_toggle_anyone(self):
        self.assertEqual(self._toggle(self.unassigned_user, 'fille').status_code, 403)
        self.assertEqual(self._toggle(self.unassigned_user, 'fils').status_code, 403)

    def test_parent_can_toggle_any_person(self):
        for person in ('fille', 'fils', 'maman', 'papa'):
            self.assertEqual(self._toggle(self.parent, person).status_code, 200, person)

    def test_assigned_kid_can_time_own_task_only(self):
        self.client.force_login(self.fille_user)
        own = self.client.post(reverse('timer_task'), {
            'person': 'fille', 'task_id': 'reveil', 'day': self.day, 'action': 'start',
        })
        self.assertEqual(own.status_code, 200)
        self.client.force_login(self.fille_user)
        sibling = self.client.post(reverse('timer_task'), {
            'person': 'fils', 'task_id': 'reveil', 'day': self.day, 'action': 'start',
        })
        self.assertEqual(sibling.status_code, 403)

    def test_unassigned_kid_cannot_time_anyone(self):
        self.client.force_login(self.unassigned_user)
        resp = self.client.post(reverse('timer_task'), {
            'person': 'fille', 'task_id': 'reveil', 'day': self.day, 'action': 'start',
        })
        self.assertEqual(resp.status_code, 403)

    def test_assigned_kid_can_reorder_own_tasks_only(self):
        self.client.force_login(self.fille_user)
        own = self.client.post(reverse('reorder_tasks'), {
            'person': 'fille', 'task_ids[]': ['reveil', 'lit'],
        })
        self.assertEqual(own.status_code, 200)
        self.client.force_login(self.fille_user)
        sibling = self.client.post(reverse('reorder_tasks'), {
            'person': 'fils', 'task_ids[]': ['reveil'],
        })
        self.assertEqual(sibling.status_code, 403)

    def test_unassigned_kid_cannot_reorder_anyone(self):
        self.client.force_login(self.unassigned_user)
        resp = self.client.post(reverse('reorder_tasks'), {'person': 'fille', 'task_ids[]': ['reveil']})
        self.assertEqual(resp.status_code, 403)

    def test_today_view_shows_only_the_assigned_kid_card(self):
        self.client.force_login(self.fille_user)
        resp = self.client.get(reverse('today'))
        self.assertEqual(resp.status_code, 200)
        kid_cards = resp.context['kid_cards']
        self.assertEqual([c['person'] for c in kid_cards], ['fille'])
        self.assertTrue(kid_cards[0]['checkable_by_viewer'])
        self.assertEqual(resp.context['parent_cards'], [])

    def test_today_view_unassigned_kid_sees_both_kids_read_only(self):
        self.client.force_login(self.unassigned_user)
        resp = self.client.get(reverse('today'))
        kid_cards = resp.context['kid_cards']
        self.assertEqual(sorted(c['person'] for c in kid_cards), ['fille', 'fils'])
        self.assertTrue(all(not c['checkable_by_viewer'] for c in kid_cards))
        self.assertTrue(resp.context['kid_unassigned'])

    def test_today_view_parent_sees_everyone_by_default(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertEqual(sorted(c['person'] for c in resp.context['kid_cards']), ['fille', 'fils'])
        self.assertEqual(sorted(c['person'] for c in resp.context['parent_cards']), ['maman', 'papa'])
        self.assertFalse(resp.context['kid_unassigned'])


class SetMemberKidTests(TestCase):
    """Only a parent may assign FamilyMembership.kid_person — a kid account granting itself
    (or a sibling) access would defeat the permission fix above entirely."""

    def setUp(self):
        self.family = Family.objects.create(name='AssignFam', invite_code='ASSIGNF1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('assignparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.child_user = User.objects.create_user('assignchild', password='pass12345')
        self.child_membership = FamilyMembership.objects.create(
            user=self.child_user, family=self.family, role='enfants'
        )

    def test_parent_can_assign_kid_person(self):
        self.client.force_login(self.parent)
        resp = self.client.post(
            reverse('set_member_kid', args=[self.child_membership.id]), {'kid_person': 'fille'}
        )
        self.assertEqual(resp.status_code, 302)
        self.child_membership.refresh_from_db()
        self.assertEqual(self.child_membership.kid_person, 'fille')

    def test_parent_can_clear_assignment(self):
        self.child_membership.kid_person = 'fille'
        self.child_membership.save()
        self.client.force_login(self.parent)
        resp = self.client.post(
            reverse('set_member_kid', args=[self.child_membership.id]), {'kid_person': ''}
        )
        self.assertEqual(resp.status_code, 302)
        self.child_membership.refresh_from_db()
        self.assertEqual(self.child_membership.kid_person, '')

    def test_invalid_kid_is_rejected(self):
        self.client.force_login(self.parent)
        resp = self.client.post(
            reverse('set_member_kid', args=[self.child_membership.id]), {'kid_person': 'papa'}
        )
        self.assertEqual(resp.status_code, 302)
        self.child_membership.refresh_from_db()
        self.assertEqual(self.child_membership.kid_person, '')

    def test_child_cannot_assign_itself(self):
        self.client.force_login(self.child_user)
        resp = self.client.post(
            reverse('set_member_kid', args=[self.child_membership.id]), {'kid_person': 'fille'}
        )
        self.assertEqual(resp.status_code, 403)
        self.child_membership.refresh_from_db()
        self.assertEqual(self.child_membership.kid_person, '')


class WhoFilterTests(TestCase):
    """The ?who= selector on Aujourd'hui (Toute la famille / Moi / chaque enfant)."""

    def setUp(self):
        self.family = Family.objects.create(name='WhoFam', invite_code='WHOFAM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('whoparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')

    def test_who_all_is_the_default(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertEqual(sorted(c['person'] for c in resp.context['kid_cards']), ['fille', 'fils'])
        self.assertEqual(sorted(c['person'] for c in resp.context['parent_cards']), ['maman', 'papa'])

    def test_who_me_shows_only_the_viewers_own_card(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'), {'who': 'me'})
        self.assertEqual(resp.context['kid_cards'], [])
        self.assertEqual([c['person'] for c in resp.context['parent_cards']], ['maman'])

    def test_who_specific_kid_shows_only_that_kid(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'), {'who': 'fils'})
        self.assertEqual([c['person'] for c in resp.context['kid_cards']], ['fils'])
        self.assertEqual(resp.context['parent_cards'], [])

    def test_invalid_who_falls_back_to_all(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'), {'who': 'bogus'})
        self.assertEqual(sorted(c['person'] for c in resp.context['kid_cards']), ['fille', 'fils'])

    def test_who_selector_not_offered_to_enfants_accounts(self):
        child = User.objects.create_user('whochild', password='pass12345')
        FamilyMembership.objects.create(user=child, family=self.family, role='enfants', kid_person='fille')
        self.client.force_login(child)
        resp = self.client.get(reverse('today'), {'who': 'fils'})
        self.assertIsNone(resp.context['who_options'])
        self.assertEqual([c['person'] for c in resp.context['kid_cards']], ['fille'])


class HomeHighlightsTests(TestCase):
    """'À venir' / 'À préparer pour demain' / 'Repas du soir' encarts on Aujourd'hui."""

    def setUp(self):
        self.family = Family.objects.create(name='HLFam', invite_code='HLFAM001')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('hlparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.today_day = DAYS[datetime.date.today().weekday()]

    def test_upcoming_activity_surfaces_a_future_activity_today(self):
        future_time = (datetime.datetime.now() + datetime.timedelta(minutes=15)).time().replace(
            second=0, microsecond=0
        )
        Activity.objects.create(
            family=self.family, person='fille', label='Piscine', day=self.today_day,
            start_time=future_time, accompanied_by='papa', location='Centre aquatique',
        )
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        upcoming = resp.context['upcoming_activity']
        self.assertIsNotNone(upcoming)
        self.assertEqual(upcoming['label'], 'Piscine')
        self.assertEqual(upcoming['accompanied_by_name'], 'Papa')
        self.assertEqual(upcoming['location'], 'Centre aquatique')

    def test_activity_already_started_is_not_upcoming(self):
        past_time = (datetime.datetime.now() - datetime.timedelta(minutes=15)).time().replace(
            second=0, microsecond=0
        )
        Activity.objects.create(
            family=self.family, person='fille', label='Passé', day=self.today_day, start_time=past_time,
        )
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertIsNone(resp.context['upcoming_activity'])

    def test_upcoming_activity_absent_on_a_non_today_day_chip(self):
        other_day = next(d for d in DAYS if d != self.today_day)
        future_time = (datetime.datetime.now() + datetime.timedelta(minutes=15)).time().replace(
            second=0, microsecond=0
        )
        Activity.objects.create(
            family=self.family, person='fille', label='Un jour', day=other_day, start_time=future_time,
        )
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'), {'day': other_day})
        self.assertIsNone(resp.context['upcoming_activity'])

    def test_tomorrow_prep_matches_generated_demain_tasks(self):
        settings = FamilySettings.load(self.family)
        holiday_today = is_zone_b_holiday(datetime.date.today())
        holiday_tomorrow = is_zone_b_holiday(datetime.date.today() + datetime.timedelta(days=1))
        expected = 0
        for person in ('fille', 'fils', 'maman', 'papa'):
            for t in tasks_for(person, self.today_day, settings, [], holiday_today, holiday_tomorrow):
                if 'demain' in t['id']:
                    expected += 1

        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertEqual(len(resp.context['tomorrow_prep']), expected)

    def test_tonight_recipe_links_to_the_days_weekly_menu_entry(self):
        recipe = Recipe.objects.create(family=self.family, name='Tajine test', category='Viande', ingredients=[])
        monday = datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())
        WeeklyMenuEntry.objects.create(family=self.family, week_start=monday, day=self.today_day, recipe=recipe)
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertEqual(resp.context['tonight_recipe'], recipe)

    def test_no_weekly_menu_entry_means_no_recipe(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertIsNone(resp.context['tonight_recipe'])
