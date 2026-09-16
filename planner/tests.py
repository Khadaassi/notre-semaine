import datetime

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException, CustomTask, DayMode,
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


class CustomTaskSettingsViewTests(TestCase):
    """Point 2/3: creating and classically editing a CustomTask from the settings page,
    with the multi-day select."""

    def setUp(self):
        self.family = Family.objects.create(name='CTV', invite_code='CTVIEWFAM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('ctparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)

    def test_add_custom_task_with_multiple_days(self):
        resp = self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fille', 'task_label': 'Piano',
            'task_days': ['lundi', 'mercredi'], 'task_period': 'soir',
        })
        self.assertEqual(resp.status_code, 302)
        task = CustomTask.objects.get(family=self.family, label='Piano')
        self.assertEqual(sorted(task.days), ['lundi', 'mercredi'])

    def test_add_custom_task_without_any_day_is_rejected(self):
        resp = self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fille', 'task_label': 'NoDays', 'task_period': 'soir',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(CustomTask.objects.filter(family=self.family, label='NoDays').exists())

    def test_edit_custom_task_updates_days_label_and_person(self):
        task = CustomTask.objects.create(
            family=self.family, person='fille', days=['lundi'], period='matin', label='Old',
        )
        resp = self.client.post(reverse('edit_custom_task', args=[task.id]), {
            'task_person': 'fils', 'task_label': 'New', 'task_days': ['mardi', 'jeudi'], 'task_period': 'soir',
        })
        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.label, 'New')
        self.assertEqual(sorted(task.days), ['jeudi', 'mardi'])
        self.assertEqual(task.person, 'fils')


class DayModeStarNonPenalizationTests(TestCase):
    """A person marked 'absence' or 'allegee' for a day ends up with fewer checkable tasks
    (see DayModeTaskFilteringTests), but that must never block their star: completing
    exactly the (smaller) checkable set still awards one — same mechanism as
    StarAwardTests.test_not_applicable_task_does_not_block_star, since
    views._checkable_ids_for recomputes the expected set dynamically either way."""

    def setUp(self):
        self.family = Family.objects.create(name='DMStar', invite_code='DAYMODESTAR1')
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

    def test_absence_day_awards_star_for_reduced_checklist(self):
        normal_ids = _checkable_ids_for(self.person, self.day, self.family)
        DayMode.objects.create(family=self.family, person=self.person, date=self.real_date, mode='absence')
        absence_ids = _checkable_ids_for(self.person, self.day, self.family)
        self.assertTrue(absence_ids)
        self.assertLess(len(absence_ids), len(normal_ids))

        self._complete(absence_ids)
        _award_star_if_day_complete(self.family, self.person, self.day, self.real_date)

        self.assertTrue(
            StarAward.objects.filter(family=self.family, person=self.person, date=self.real_date).exists()
        )
        self.assertEqual(KidStars.objects.get(family=self.family, person=self.person).total, 1)

    def test_allegee_day_awards_star_for_reduced_checklist(self):
        normal_ids = _checkable_ids_for(self.person, self.day, self.family)
        DayMode.objects.create(family=self.family, person=self.person, date=self.real_date, mode='allegee')
        allegee_ids = _checkable_ids_for(self.person, self.day, self.family)
        self.assertTrue(allegee_ids)
        self.assertLess(len(allegee_ids), len(normal_ids))

        self._complete(allegee_ids)
        _award_star_if_day_complete(self.family, self.person, self.day, self.real_date)

        self.assertTrue(
            StarAward.objects.filter(family=self.family, person=self.person, date=self.real_date).exists()
        )


class TaskExceptionUITests(TestCase):
    """Point 1: creating disabled_once/disabled_from/not_applicable TaskException rows from
    'Aujourd'hui', listing/reactivating them from settings, and restricting the whole thing
    to parents."""

    def setUp(self):
        self.family = Family.objects.create(name='TEX', invite_code='TEXFAM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('texparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='papa')
        self.client.force_login(self.parent)
        self.day = 'lundi'
        self.real_date = _real_date_for_day(self.day)

    def test_create_disabled_once_removes_task_for_that_day_only(self):
        checkable = _checkable_ids_for('fille', self.day, self.family)
        task_id = sorted(checkable)[0]
        resp = self.client.post(reverse('create_task_exception'), {
            'person': 'fille', 'task_id': task_id, 'day': self.day, 'kind': 'disabled_once',
        })
        self.assertEqual(resp.status_code, 302)
        remaining = _checkable_ids_for('fille', self.day, self.family)
        self.assertNotIn(task_id, remaining)

    def test_reactivate_removes_the_exception_and_restores_the_task(self):
        exc = TaskException.objects.create(
            family=self.family, person='fille', task_id='lit', kind='disabled_once', date=self.real_date,
        )
        self.assertNotIn('lit', _checkable_ids_for('fille', self.day, self.family))
        resp = self.client.post(reverse('reactivate_task_exception', args=[exc.id]))
        self.assertEqual(resp.status_code, 302)
        exc.refresh_from_db()
        self.assertFalse(exc.active)
        self.assertIn('lit', _checkable_ids_for('fille', self.day, self.family))

    def test_child_cannot_create_task_exception(self):
        child = User.objects.create_user('texchild', password='pass12345')
        FamilyMembership.objects.create(user=child, family=self.family, role='enfants')
        self.client.force_login(child)
        resp = self.client.post(reverse('create_task_exception'), {
            'person': 'fille', 'task_id': 'lit', 'day': self.day, 'kind': 'disabled_once',
        })
        self.assertEqual(resp.status_code, 403)


class SetDayModeTests(TestCase):
    """Point 4's UI plumbing: setting/clearing a person's DayMode for one day from
    'Aujourd'hui', restricted to parents."""

    def setUp(self):
        self.family = Family.objects.create(name='SDM', invite_code='SETDAYMODE1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('sdmparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='papa')
        self.client.force_login(self.parent)
        self.day = 'lundi'
        self.real_date = _real_date_for_day(self.day)

    def test_set_day_mode_creates_a_daymode_row(self):
        resp = self.client.post(reverse('set_day_mode'), {
            'person': 'fille', 'day': self.day, 'mode': 'absence',
        })
        self.assertEqual(resp.status_code, 302)
        dm = DayMode.objects.get(family=self.family, person='fille', date=self.real_date)
        self.assertEqual(dm.mode, 'absence')

    def test_setting_mode_back_to_normal_deletes_the_row(self):
        DayMode.objects.create(family=self.family, person='fille', date=self.real_date, mode='absence')
        resp = self.client.post(reverse('set_day_mode'), {
            'person': 'fille', 'day': self.day, 'mode': 'normal',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            DayMode.objects.filter(family=self.family, person='fille', date=self.real_date).exists()
        )

    def test_child_cannot_set_day_mode(self):
        child = User.objects.create_user('sdmchild', password='pass12345')
        FamilyMembership.objects.create(user=child, family=self.family, role='enfants')
        self.client.force_login(child)
        resp = self.client.post(reverse('set_day_mode'), {
            'person': 'fille', 'day': self.day, 'mode': 'absence',
        })
        self.assertEqual(resp.status_code, 403)


class TodayAndSettingsPagesRenderWithNewUITests(TestCase):
    """Smoke-tests the new per-task/per-card controls actually render: a parent sees the
    'Gérer les tâches' toggle and the day-mode picker, a child sees neither (routines-v2
    management stays parent-only), and settings lists the new exceptions card."""

    def setUp(self):
        self.family = Family.objects.create(name='TP', invite_code='TODAYPAGE1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('tpparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.child = User.objects.create_user('tpchild', password='pass12345')
        FamilyMembership.objects.create(user=self.child, family=self.family, role='enfants')

    def test_today_page_renders_for_parent_with_exception_controls(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertEqual(resp.status_code, 200)
        # The toggle button's id is only rendered inside {% if is_parent %} — text alone
        # isn't a safe marker here since the page's own JS mentions the same label.
        self.assertContains(resp, 'id="exceptionToggle"')
        self.assertContains(resp, 'Mode du jour')

    def test_today_page_renders_for_child_without_exception_controls(self):
        self.client.force_login(self.child)
        resp = self.client.get(reverse('today'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="exceptionToggle"')
        self.assertNotContains(resp, 'Mode du jour')

    def test_settings_page_lists_task_exceptions_card(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('settings'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Exceptions de tâches')


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


class ReassignTaskTests(TestCase):
    """Point 5: handing a task off to another family member for one day, via
    TaskException(kind='reassigned') — see views._reassignment_maps/_apply_task_overrides."""

    def setUp(self):
        self.family = Family.objects.create(name='RE', invite_code='REASSIGNFAM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('reparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)
        self.day = 'lundi'
        self.real_date = _real_date_for_day(self.day)

    def test_reassigned_task_moves_from_source_to_target(self):
        source_ids = _checkable_ids_for('maman', self.day, self.family)
        task_id = sorted(source_ids)[0]
        resp = self.client.post(reverse('reassign_task'), {
            'person': 'maman', 'task_id': task_id, 'day': self.day, 'reassigned_to': 'papa',
        })
        self.assertEqual(resp.status_code, 302)

        remaining_source = _checkable_ids_for('maman', self.day, self.family)
        self.assertNotIn(task_id, remaining_source)

        target_ids = _checkable_ids_for('papa', self.day, self.family)
        self.assertIn(f'reassigned_maman_{task_id}', target_ids)

    def test_reassigned_task_can_be_completed_by_the_target_and_counts_for_their_star(self):
        source_ids = _checkable_ids_for('fille', self.day, self.family)
        task_id = sorted(source_ids)[0]
        self.client.post(reverse('reassign_task'), {
            'person': 'fille', 'task_id': task_id, 'day': self.day, 'reassigned_to': 'fils',
        })
        target_ids = _checkable_ids_for('fils', self.day, self.family)
        for tid in target_ids:
            TaskCompletion.objects.update_or_create(
                family=self.family, person='fils', date=self.real_date, task_id=tid,
                defaults={'done': True},
            )
        _award_star_if_day_complete(self.family, 'fils', self.day, self.real_date)
        self.assertTrue(
            StarAward.objects.filter(family=self.family, person='fils', date=self.real_date).exists()
        )

    def test_cannot_reassign_to_self(self):
        resp = self.client.post(reverse('reassign_task'), {
            'person': 'maman', 'task_id': 'repas', 'day': self.day, 'reassigned_to': 'maman',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TaskException.objects.filter(kind='reassigned').exists())
