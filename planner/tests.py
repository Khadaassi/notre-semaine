import datetime

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException, CustomTask,
)
from .task_logic import is_zone_b_holiday, ZONE_B_HOLIDAYS, tasks_for
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


class DayModeTaskFilteringTests(TestCase):
    """task_logic.tasks_for's day_mode wiring (routines-v2 point 4): 'absence' drops
    school/activity/work tasks but keeps the core routine; 'allegee' drops only heavy
    chores/full homework; 'vacances' folds into holiday_today."""

    def setUp(self):
        self.family = Family.objects.create(name='DM', invite_code='DAYMODEFAM1')
        self.settings = FamilySettings.load(self.family)

    def test_absence_drops_school_but_keeps_core_routine(self):
        normal_ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [])}
        absence_ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [], day_mode='absence')}
        self.assertIn('ecole', normal_ids)
        self.assertNotIn('ecole', absence_ids)
        for keep_id in ('lit', 'oudou_m', 'priere_m', 'petitdej', 'brossage_m'):
            self.assertIn(keep_id, absence_ids)
        self.assertLess(len(absence_ids), len(normal_ids))

    def test_allegee_drops_heavy_chores_but_keeps_most_tasks(self):
        normal_ids = {t['id'] for t in tasks_for('maman', 'mercredi', self.settings, [])}
        allegee_ids = {t['id'] for t in tasks_for('maman', 'mercredi', self.settings, [], day_mode='allegee')}
        self.assertIn('deepclean', normal_ids)
        self.assertIn('lessive', normal_ids)
        self.assertNotIn('deepclean', allegee_ids)
        self.assertNotIn('lessive', allegee_ids)
        self.assertIn('petitdej_famille', allegee_ids)
        self.assertLess(len(allegee_ids), len(normal_ids))

    def test_vacances_mode_behaves_like_a_holiday_for_that_person(self):
        vac_ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [], day_mode='vacances')}
        self.assertNotIn('ecole', vac_ids)
        self.assertIn('vacances', vac_ids)

    def test_normal_mode_is_a_no_op(self):
        normal_ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [])}
        explicit_normal_ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [], day_mode='normal')}
        self.assertEqual(normal_ids, explicit_normal_ids)


class CustomTaskMultiDayTests(TestCase):
    """CustomTask.days replaces the old single-value `day` (routines-v2 point 3): a task
    can recur on several week days, and task_logic.tasks_for filters on membership in that
    list rather than equality."""

    def setUp(self):
        self.family = Family.objects.create(name='CT', invite_code='CUSTOMTASKFAM1')
        self.settings = FamilySettings.load(self.family)

    def test_multi_day_custom_task_appears_on_every_selected_day_only(self):
        task = CustomTask.objects.create(
            family=self.family, person='fille', days=['lundi', 'mercredi'], period='soir', label='Piano',
        )
        lundi_ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [], custom_tasks=[task])}
        mardi_ids = {t['id'] for t in tasks_for('fille', 'mardi', self.settings, [], custom_tasks=[task])}
        mercredi_ids = {t['id'] for t in tasks_for('fille', 'mercredi', self.settings, [], custom_tasks=[task])}
        self.assertIn(f'custom_{task.id}', lundi_ids)
        self.assertIn(f'custom_{task.id}', mercredi_ids)
        self.assertNotIn(f'custom_{task.id}', mardi_ids)

    def test_days_display_lists_selected_days_in_order(self):
        task = CustomTask.objects.create(
            family=self.family, person='fille', days=['mercredi', 'lundi'], period='matin', label='Test',
        )
        self.assertEqual(task.days_display(), 'Mercredi, Lundi')



class CustomTaskDaysDataMigrationTests(TransactionTestCase):
    """Exercises the CustomTask day -> days data migration (0016_customtask_populate_days)
    end to end using Django's documented MigrationExecutor pattern, so it runs the actual
    migration code against a real (non-empty) table rather than re-implementing the
    conversion — see also the manual empty-DB and populated-DB checks performed while
    building this migration (python manage.py migrate on a fresh and on a seeded sqlite
    file), which this test automates for the populated-DB case."""

    def test_existing_day_value_becomes_a_singleton_days_list(self):
        migrate_from = [('planner', '0015_customtask_add_days')]
        migrate_to = [('planner', '0017_customtask_remove_day')]

        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        try:
            old_apps = executor.loader.project_state(migrate_from).apps
            OldFamily = old_apps.get_model('planner', 'Family')
            OldCustomTask = old_apps.get_model('planner', 'CustomTask')
            family = OldFamily.objects.create(name='Mig', invite_code='MIGCODE1')
            OldCustomTask.objects.create(
                family=family, person='fille', day='lundi', period='matin', label='Legacy', days=[]
            )

            executor = MigrationExecutor(connection)
            executor.migrate(migrate_to)
            new_apps = executor.loader.project_state(migrate_to).apps
            NewCustomTask = new_apps.get_model('planner', 'CustomTask')
            task = NewCustomTask.objects.get(label='Legacy')
            self.assertEqual(task.days, ['lundi'])
        finally:
            # Bring the schema all the way back to the latest state so tests that run
            # after this one in the same process see the real, current models.
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
            django_apps.clear_cache()
