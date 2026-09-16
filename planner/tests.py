import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException,
)
from .task_logic import is_zone_b_holiday, ZONE_B_HOLIDAYS
from .views import _checkable_ids_for, _award_star_if_day_complete, _real_date_for_day, _level_for


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


class ConfigurableStarMilestoneTests(TestCase):
    """FamilySettings.star_milestone must drive the surprise threshold and the level badge
    per family, instead of the old fixed STAR_MILESTONE=15 constant."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='MILESTEST1')
        self.settings = FamilySettings.load(self.family)
        self.settings.star_milestone = 3
        self.settings.star_reward_text = 'Une glace au choix !'
        self.settings.save()
        self.day = 'lundi'
        self.person = 'fille'
        self.checkable_ids = _checkable_ids_for(self.person, self.day, self.family)
        self.assertTrue(self.checkable_ids)

    def _complete_day(self, real_date):
        for task_id in self.checkable_ids:
            TaskCompletion.objects.update_or_create(
                family=self.family, person=self.person, date=real_date, task_id=task_id,
                defaults={'done': True},
            )
        return _award_star_if_day_complete(self.family, self.person, self.day, real_date)

    def test_milestone_reached_at_configured_threshold_not_fifteen(self):
        base_date = _real_date_for_day(self.day)
        results = [self._complete_day(base_date - datetime.timedelta(days=7 * i)) for i in range(3)]

        milestone_flags = [r[0] for r in results]
        # Only the 3rd fully-completed day (our custom threshold) should trigger the popup.
        self.assertEqual(milestone_flags, [False, False, True])
        self.assertEqual(results[-1][1], 3)  # cumulative stars total
        self.assertEqual(results[-1][2], 'Une glace au choix !')  # parent-defined reward text

    def test_reward_text_absent_when_milestone_not_reached(self):
        base_date = _real_date_for_day(self.day)
        milestone_reached, _, reward_text = self._complete_day(base_date)
        self.assertFalse(milestone_reached)
        self.assertIsNone(reward_text)

    def test_default_generic_reward_when_no_custom_text_set(self):
        self.settings.star_reward_text = ''
        self.settings.save()
        base_date = _real_date_for_day(self.day)
        results = [self._complete_day(base_date - datetime.timedelta(days=7 * i)) for i in range(3)]
        self.assertTrue(results[-1][0])
        self.assertIsNone(results[-1][2])  # no custom text -> template falls back to MILESTONE_MSG

    def test_level_uses_family_milestone_not_global_constant(self):
        # stars_per_level = milestone * 6 = 18 when star_milestone == 3.
        self.assertEqual(_level_for(17, self.settings.star_milestone), 1)
        self.assertEqual(_level_for(18, self.settings.star_milestone), 2)


class SettingsRewardFormTests(TestCase):
    """Covers the new 'Récompenses' form in settings.html: updating the reward
    threshold/text — parent-only, like every other settings form."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='SETFORM1')
        self.parent_user = User.objects.create_user('parentform', password='pass12345')
        FamilyMembership.objects.create(user=self.parent_user, family=self.family, role='maman')
        self.settings = FamilySettings.load(self.family)

    def test_parent_can_update_reward_settings(self):
        self.client.force_login(self.parent_user)
        resp = self.client.post(reverse('settings'), {
            'save_rewards': '1', 'star_milestone': '7', 'star_reward_text': 'Un ciné !',
        })
        self.assertEqual(resp.status_code, 302)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.star_milestone, 7)
        self.assertEqual(self.settings.star_reward_text, 'Un ciné !')
