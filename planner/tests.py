import datetime
import importlib
import re
from decimal import Decimal

from django.apps import apps
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .forms import RecipeForm, parse_ingredients_text
from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException, Activity, Recipe, GroceryItem, WeeklyMenuEntry,
    CustomTask, DayMode,
)
from .task_logic import (
    is_zone_b_holiday, ZONE_B_HOLIDAYS, DAYS, SCHOOL_DAYS, WEEKEND_DAYS, tasks_for,
    find_schedule_conflicts, occurs_on, activities_on, phase_for_time,
)
from .views import (
    _checkable_ids_for, _award_star_if_day_complete, _real_date_for_day, _level_for, _monday_of,
)

_ingredient_migration = importlib.import_module('planner.migrations.0016_migrate_ingredient_format')


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


class StarsTrackerGridTests(TestCase):
    """The Étoiles grid fills in the order completed days actually happened, one cell per
    star earned this cycle — not a fixed calendar window with gaps for missed days."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='GRIDTEST1')
        self.settings = FamilySettings.load(self.family)
        self.settings.star_milestone = 5
        self.settings.save()
        self.parent = User.objects.create_user('gridparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.day = 'lundi'
        self.person = 'fille'
        self.checkable_ids = _checkable_ids_for(self.person, self.day, self.family)

    def _complete_day(self, real_date):
        for task_id in self.checkable_ids:
            TaskCompletion.objects.update_or_create(
                family=self.family, person=self.person, date=real_date, task_id=task_id,
                defaults={'done': True},
            )
        return _award_star_if_day_complete(self.family, self.person, self.day, real_date)

    def _fille_tracker(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('stars'))
        return next(t for t in resp.context['trackers'] if t['person'] == 'fille')

    def test_fresh_cycle_has_no_filled_cells(self):
        tracker = self._fille_tracker()
        self.assertEqual(len(tracker['days']), 5)
        self.assertFalse(any(d['filled'] for d in tracker['days']))

    def test_cells_fill_in_completion_order_skipping_a_missed_day(self):
        base = _real_date_for_day(self.day)
        day1, day3, day5 = base, base + datetime.timedelta(days=2), base + datetime.timedelta(days=4)
        # day2 (base+1) is deliberately never completed — a missed day.
        self._complete_day(day1)
        self._complete_day(day3)
        self._complete_day(day5)

        tracker = self._fille_tracker()
        filled = [d for d in tracker['days'] if d['filled']]
        self.assertEqual([d['date'] for d in filled], [day1, day3, day5])
        # The 3 filled cells are the first 3 in the grid — no empty gap for the missed day.
        self.assertEqual([d['filled'] for d in tracker['days']], [True, True, True, False, False])

    def test_grid_length_matches_family_milestone(self):
        self.settings.star_milestone = 8
        self.settings.save()
        tracker = self._fille_tracker()
        self.assertEqual(len(tracker['days']), 8)


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


class SettingsTabletTokenFormTests(TestCase):
    """Covers the new 'Affichage tablette cuisine' form in settings.html: regenerating the
    kitchen-tablet token — parent-only, like every other settings form."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='TOKFORM1')
        self.parent_user = User.objects.create_user('parenttoken', password='pass12345')
        FamilyMembership.objects.create(user=self.parent_user, family=self.family, role='maman')
        self.settings = FamilySettings.load(self.family)

    def test_parent_can_regenerate_tablet_token(self):
        old_token = self.settings.tablet_token
        self.assertTrue(old_token)
        self.client.force_login(self.parent_user)
        resp = self.client.post(reverse('settings'), {'regenerate_tablet_token': '1'})
        self.assertEqual(resp.status_code, 302)
        self.settings.refresh_from_db()
        self.assertTrue(self.settings.tablet_token)
        self.assertNotEqual(self.settings.tablet_token, old_token)


class TabletViewTests(TestCase):
    """The kitchen-tablet display needs no login, but must refuse an invalid/absent token."""

    def setUp(self):
        self.family = Family.objects.create(name='Test', invite_code='TABLETFAM1')
        self.settings = FamilySettings.load(self.family)

    def test_invalid_token_is_refused(self):
        resp = self.client.get('/tablette/not-a-real-token/')
        self.assertEqual(resp.status_code, 404)

    def test_missing_token_is_refused(self):
        resp = self.client.get('/tablette/')
        self.assertEqual(resp.status_code, 404)

    def test_valid_token_renders_without_login(self):
        url = reverse('tablet', args=[self.settings.tablet_token])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Repas du soir')
        self.assertContains(resp, 'Prochains événements')
        # No JS wiring for checkbox toggling / reordering is shipped on this read-only page.
        self.assertNotContains(resp, 'toggle-task')
        self.assertNotContains(resp, 'reorderToggle')

    def test_day_query_param_navigates_without_auth(self):
        url = reverse('tablet', args=[self.settings.tablet_token])
        resp = self.client.get(url, {'day': 'mardi'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['day'], 'mardi')


class IngredientFormatToleranceTests(TestCase):
    """Recipe.normalize_ingredients()/ingredients_list() must read both the pre-Lot-4
    format (plain list of name strings) and the current {"name","quantity","unit"} dict
    format, since real recipes migrated by 0016 and any row that somehow slips through
    unmigrated must both render correctly."""

    def test_normalize_ingredients_accepts_old_string_list(self):
        self.assertEqual(
            Recipe.normalize_ingredients(['Poulet', 'Riz']),
            [{'name': 'Poulet', 'quantity': None, 'unit': ''},
             {'name': 'Riz', 'quantity': None, 'unit': ''}],
        )

    def test_normalize_ingredients_accepts_new_dict_list(self):
        self.assertEqual(
            Recipe.normalize_ingredients([{'name': 'Poulet', 'quantity': 500, 'unit': 'g'}]),
            [{'name': 'Poulet', 'quantity': 500, 'unit': 'g'}],
        )

    def test_normalize_ingredients_drops_blank_entries(self):
        self.assertEqual(Recipe.normalize_ingredients(['', '  ', {'name': ''}, None]), [])

    def test_ingredients_display_formats_quantity_and_unit(self):
        recipe = Recipe(ingredients=[
            {'name': 'Poulet', 'quantity': 500, 'unit': 'g'},
            {'name': 'Citron', 'quantity': None, 'unit': ''},
        ])
        self.assertEqual(recipe.ingredients_display(), ['Poulet — 500 g', 'Citron'])


class IngredientDataMigrationTests(TestCase):
    """Exercises the real 0016 migration functions against live data — both an empty-ish
    case (idempotent no-op on already-migrated rows) and a family with existing
    old-format recipes (the real-world case: prod already has recipes stored as plain
    string lists)."""

    def setUp(self):
        self.family = Family.objects.create(name='Migration', invite_code='MIGCODE01')

    def test_forward_migration_converts_old_string_format(self):
        recipe = Recipe.objects.create(
            family=self.family, name='Ancienne recette', category='Autre',
            ingredients=['Poulet', 'Riz', ''],
        )
        _ingredient_migration.convert_ingredients_forward(apps, None)
        recipe.refresh_from_db()
        self.assertEqual(recipe.ingredients, [
            {'name': 'Poulet', 'quantity': None, 'unit': ''},
            {'name': 'Riz', 'quantity': None, 'unit': ''},
        ])

    def test_forward_migration_is_idempotent_on_already_migrated_rows(self):
        recipe = Recipe.objects.create(
            family=self.family, name='Nouvelle recette', category='Autre',
            ingredients=[{'name': 'Saumon', 'quantity': 200, 'unit': 'g'}],
        )
        _ingredient_migration.convert_ingredients_forward(apps, None)
        recipe.refresh_from_db()
        self.assertEqual(recipe.ingredients, [{'name': 'Saumon', 'quantity': 200, 'unit': 'g'}])

    def test_forward_migration_on_empty_database_is_a_no_op(self):
        # No Recipe rows at all — must not raise.
        Recipe.objects.all().delete()
        _ingredient_migration.convert_ingredients_forward(apps, None)
        self.assertEqual(Recipe.objects.count(), 0)

    def test_backward_migration_collapses_dicts_to_name_list(self):
        recipe = Recipe.objects.create(
            family=self.family, name='Recette', category='Autre',
            ingredients=[{'name': 'Poulet', 'quantity': 500, 'unit': 'g'}],
        )
        _ingredient_migration.convert_ingredients_backward(apps, None)
        recipe.refresh_from_db()
        self.assertEqual(recipe.ingredients, ['Poulet'])


class ParseIngredientsTextTests(TestCase):
    """Covers the classic regex parser behind the recipe form's ingredients field —
    no AI/LLM involved, per the task constraints."""

    def test_parses_quantity_and_unit(self):
        self.assertEqual(
            parse_ingredients_text('Poulet 500g, Riz 200 g, Citron'),
            [
                {'name': 'Poulet', 'quantity': 500.0, 'unit': 'g'},
                {'name': 'Riz', 'quantity': 200.0, 'unit': 'g'},
                {'name': 'Citron', 'quantity': None, 'unit': ''},
            ],
        )

    def test_parses_decimal_dot_and_word_unit(self):
        # Decimals must use '.', not ',' — ',' is already the ingredient separator, so
        # a French-style decimal comma would be split into two segments beforehand.
        self.assertEqual(
            parse_ingredients_text("Huile d'olive 1.5 cuillère"),
            [{'name': "Huile d'olive", 'quantity': 1.5, 'unit': 'cuillère'}],
        )

    def test_blank_and_whitespace_segments_are_ignored(self):
        self.assertEqual(parse_ingredients_text('Poulet 500g,, , Riz'),
                          [{'name': 'Poulet', 'quantity': 500.0, 'unit': 'g'},
                           {'name': 'Riz', 'quantity': None, 'unit': ''}])


class RecipeFormTests(TestCase):
    def test_save_parses_ingredients_and_steps(self):
        form = RecipeForm(data={
            'name': 'Nouvelle recette', 'category': 'Autre',
            'ingredients_text': 'Poulet 500g, Riz 200g, Citron',
            'steps_text': "Étape un\nÉtape deux",
            'duration_minutes': '25',
        })
        self.assertTrue(form.is_valid(), form.errors)
        recipe = form.save(commit=False)
        self.assertEqual(recipe.ingredients, [
            {'name': 'Poulet', 'quantity': 500.0, 'unit': 'g'},
            {'name': 'Riz', 'quantity': 200.0, 'unit': 'g'},
            {'name': 'Citron', 'quantity': None, 'unit': ''},
        ])
        self.assertEqual(recipe.steps, ['Étape un', 'Étape deux'])
        self.assertEqual(recipe.duration_minutes, 25)


class AggregateIngredientsTests(TestCase):
    """Covers Recipe.aggregate_ingredients: the quantity-summing behind
    menu.copy_to_courses (Lot 4, point 1)."""

    def setUp(self):
        self.family = Family.objects.create(name='Agg', invite_code='AGGCODE01')

    def test_sums_same_name_and_unit_across_recipes_case_insensitively(self):
        r1 = Recipe.objects.create(family=self.family, name='R1', category='Autre',
                                    ingredients=[{'name': 'Riz', 'quantity': 200, 'unit': 'g'}])
        r2 = Recipe.objects.create(family=self.family, name='R2', category='Autre',
                                    ingredients=[{'name': 'riz', 'quantity': 100, 'unit': 'g'}])
        result = Recipe.aggregate_ingredients([r1, r2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['name'], 'Riz')
        self.assertEqual(result[0]['quantity'], Decimal('300'))

    def test_old_format_recipe_contributes_unknown_quantity(self):
        r1 = Recipe.objects.create(family=self.family, name='R1', category='Autre',
                                    ingredients=[{'name': 'Citron', 'quantity': 2, 'unit': ''}])
        r2 = Recipe.objects.create(family=self.family, name='R2', category='Autre',
                                    ingredients=['Citron'])  # old string-list format
        result = Recipe.aggregate_ingredients([r1, r2])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]['quantity'])

    def test_different_units_are_kept_separate(self):
        r1 = Recipe.objects.create(family=self.family, name='R1', category='Autre',
                                    ingredients=[{'name': 'Lait', 'quantity': 200, 'unit': 'ml'}])
        r2 = Recipe.objects.create(family=self.family, name='R2', category='Autre',
                                    ingredients=[{'name': 'Lait', 'quantity': 1, 'unit': 'L'}])
        result = Recipe.aggregate_ingredients([r1, r2])
        self.assertEqual(len(result), 2)


class CopyToCoursesTests(TestCase):
    """Covers menu.copy_to_courses: quantity aggregation into GroceryItem, and the
    explicit (non-silent) handling of an ingredient that comes back while its
    GroceryItem is still checked from a previous week (Lot 4, point 5)."""

    def setUp(self):
        self.family = Family.objects.create(name='Courses', invite_code='COURSECODE1')
        FamilySettings.load(self.family)
        self.user = User.objects.create_user('parentcourses', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)
        self.recipe1 = Recipe.objects.create(
            family=self.family, name='Recette A', category='Autre',
            ingredients=[{'name': 'Riz', 'quantity': 200, 'unit': 'g'},
                         {'name': 'Poulet', 'quantity': 300, 'unit': 'g'}],
        )
        self.recipe2 = Recipe.objects.create(
            family=self.family, name='Recette B', category='Autre',
            ingredients=[{'name': 'Riz', 'quantity': 100, 'unit': 'g'}],
        )
        week_start = _monday_of(datetime.date.today())
        WeeklyMenuEntry.objects.create(family=self.family, week_start=week_start, day='lundi', recipe=self.recipe1)
        WeeklyMenuEntry.objects.create(family=self.family, week_start=week_start, day='mardi', recipe=self.recipe2)

    def test_copy_to_courses_sums_quantities_for_shared_ingredient(self):
        resp = self.client.post(reverse('menu'), {'copy_to_courses': '1'})
        self.assertEqual(resp.status_code, 302)
        rice = GroceryItem.objects.get(family=self.family, name='Riz')
        self.assertEqual(rice.quantity, Decimal('300.00'))
        self.assertEqual(rice.unit, 'g')
        self.assertEqual(rice.category, 'Menu de la semaine')
        chicken = GroceryItem.objects.get(family=self.family, name='Poulet')
        self.assertEqual(chicken.quantity, Decimal('300.00'))

    def test_returning_checked_item_requires_explicit_confirmation(self):
        GroceryItem.objects.create(
            family=self.family, name='Riz', category='Menu de la semaine', checked=True,
        )
        resp = self.client.post(reverse('menu'), {'copy_to_courses': '1'})
        # No redirect: the confirmation screen is rendered instead of silently acting.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Riz')
        rice = GroceryItem.objects.get(family=self.family, name='Riz')
        self.assertTrue(rice.checked)  # untouched until the user resolves the conflict

    def test_confirmation_uncheck_resolution_marks_item_to_buy_again(self):
        GroceryItem.objects.create(
            family=self.family, name='Riz', category='Menu de la semaine', checked=True,
        )
        resp = self.client.post(
            reverse('menu'), {'copy_to_courses': '1', 'resolve_returning': 'uncheck'}
        )
        self.assertEqual(resp.status_code, 302)
        rice = GroceryItem.objects.get(family=self.family, name='Riz')
        self.assertFalse(rice.checked)
        self.assertEqual(rice.quantity, Decimal('300.00'))

    def test_confirmation_keep_resolution_leaves_item_checked(self):
        GroceryItem.objects.create(
            family=self.family, name='Riz', category='Menu de la semaine', checked=True,
        )
        resp = self.client.post(
            reverse('menu'), {'copy_to_courses': '1', 'resolve_returning': 'keep'}
        )
        self.assertEqual(resp.status_code, 302)
        rice = GroceryItem.objects.get(family=self.family, name='Riz')
        self.assertTrue(rice.checked)

    def test_unchecked_existing_item_is_updated_without_confirmation(self):
        GroceryItem.objects.create(
            family=self.family, name='Riz', category='Ajoutés', checked=False,
        )
        resp = self.client.post(reverse('menu'), {'copy_to_courses': '1'})
        self.assertEqual(resp.status_code, 302)
        rice = GroceryItem.objects.get(family=self.family, name='Riz')
        self.assertEqual(rice.quantity, Decimal('300.00'))
        self.assertEqual(rice.category, 'Ajoutés')  # category untouched on an existing item


class GroceryItemManagementTests(TestCase):
    """Covers edit_grocery/delete_grocery/toggle_grocery_home (Lot 4, points 2 & 4)."""

    def setUp(self):
        self.family = Family.objects.create(name='Grocery', invite_code='GROCCODE01')
        FamilySettings.load(self.family)
        self.user = User.objects.create_user('parentgrocery', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)
        self.item = GroceryItem.objects.create(family=self.family, name='Yaourts', category='Ajoutés')

    def test_toggle_already_home(self):
        resp = self.client.post(
            reverse('toggle_grocery_home'), {'item_id': self.item.id, 'already_home': '1'}
        )
        self.assertEqual(resp.status_code, 200)
        self.item.refresh_from_db()
        self.assertTrue(self.item.already_home)

    def test_edit_grocery_updates_fields(self):
        resp = self.client.post(reverse('edit_grocery'), {
            'item_id': self.item.id, 'name': 'Yaourts nature', 'category': 'Produits laitiers',
            'quantity': '4', 'unit': 'pots',
        })
        self.assertEqual(resp.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'Yaourts nature')
        self.assertEqual(self.item.category, 'Produits laitiers')
        self.assertEqual(self.item.quantity, Decimal('4'))
        self.assertEqual(self.item.unit, 'pots')

    def test_edit_grocery_rejects_blank_name(self):
        resp = self.client.post(reverse('edit_grocery'), {'item_id': self.item.id, 'name': '  '})
        self.assertEqual(resp.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.name, 'Yaourts')  # unchanged

    def test_delete_grocery_removes_item(self):
        resp = self.client.post(reverse('delete_grocery'), {'item_id': self.item.id})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(GroceryItem.objects.filter(id=self.item.id).exists())


class RecipeFavoriteTests(TestCase):
    def setUp(self):
        self.family = Family.objects.create(name='Favorite', invite_code='FAVCODE01')
        FamilySettings.load(self.family)
        self.user = User.objects.create_user('parentfav', password='pass12345')
        FamilyMembership.objects.create(user=self.user, family=self.family, role='maman')
        self.client.force_login(self.user)
        self.recipe = Recipe.objects.create(family=self.family, name='Recette C', category='Autre', ingredients=[])

    def test_toggle_favorite(self):
        resp = self.client.post(
            reverse('toggle_recipe_favorite'), {'recipe_id': self.recipe.id, 'is_favorite': '1'}
        )
        self.assertEqual(resp.status_code, 200)
        self.recipe.refresh_from_db()
        self.assertTrue(self.recipe.is_favorite)

    def test_menu_favoris_filter_only_shows_favorites(self):
        # Note: the per-day recipe dropdown always lists every recipe regardless of this
        # filter (you should be able to assign any recipe to a day even while filtering
        # the "Recettes enregistrées" list) — so this checks the filtered listing
        # (by_cat) specifically, not the whole rendered page.
        Recipe.objects.create(family=self.family, name='Recette D', category='Autre', ingredients=[])
        self.recipe.is_favorite = True
        self.recipe.save()
        resp = self.client.get(reverse('menu'), {'favoris': '1'})
        display_names = [r.name for items in resp.context['by_cat'].values() for r in items]
        self.assertIn('Recette C', display_names)
        self.assertNotIn('Recette D', display_names)

class SharedKidAccountTests(TestCase):
    """The 'enfants' account is shared by every kid in the family — they're together on one
    screen doing tasks at the same time — so it must be able to see and act on every kid's
    card, never restricted to just one."""

    def setUp(self):
        self.family = Family.objects.create(name='KidPerm', invite_code='KIDPERM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('kpparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.kid_user = User.objects.create_user('kpkid', password='pass12345')
        FamilyMembership.objects.create(user=self.kid_user, family=self.family, role='enfants')
        self.day = 'lundi'

    def _toggle(self, user, person, task_id='reveil'):
        self.client.force_login(user)
        return self.client.post(reverse('toggle_task'), {
            'person': person, 'task_id': task_id, 'day': self.day, 'done': '1',
        })

    def test_shared_kid_account_can_toggle_either_kids_task(self):
        self.assertEqual(self._toggle(self.kid_user, 'fille').status_code, 200)
        self.assertEqual(self._toggle(self.kid_user, 'fils').status_code, 200)

    def test_shared_kid_account_cannot_toggle_a_parents_task(self):
        resp = self._toggle(self.kid_user, 'maman')
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            TaskCompletion.objects.filter(family=self.family, person='maman', task_id='reveil').exists()
        )

    def test_parent_can_toggle_any_person(self):
        for person in ('fille', 'fils', 'maman', 'papa'):
            self.assertEqual(self._toggle(self.parent, person).status_code, 200, person)

    def test_shared_kid_account_can_time_either_kids_task(self):
        self.client.force_login(self.kid_user)
        for person in ('fille', 'fils'):
            resp = self.client.post(reverse('timer_task'), {
                'person': person, 'task_id': 'reveil', 'day': self.day, 'action': 'start',
            })
            self.assertEqual(resp.status_code, 200, person)

    def test_shared_kid_account_can_reorder_either_kids_tasks(self):
        self.client.force_login(self.kid_user)
        for person in ('fille', 'fils'):
            resp = self.client.post(reverse('reorder_tasks'), {
                'person': person, 'task_ids[]': ['reveil', 'lit'],
            })
            self.assertEqual(resp.status_code, 200, person)

    def test_today_view_shows_both_kid_cards_fully_checkable(self):
        self.client.force_login(self.kid_user)
        resp = self.client.get(reverse('today'))
        self.assertEqual(resp.status_code, 200)
        kid_cards = resp.context['kid_cards']
        self.assertEqual(sorted(c['person'] for c in kid_cards), ['fille', 'fils'])
        self.assertTrue(all(c['checkable_by_viewer'] for c in kid_cards))
        self.assertEqual(resp.context['parent_cards'], [])

    def test_today_view_parent_sees_everyone_by_default(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('today'))
        self.assertEqual(sorted(c['person'] for c in resp.context['kid_cards']), ['fille', 'fils'])
        self.assertEqual(sorted(c['person'] for c in resp.context['parent_cards']), ['maman', 'papa'])


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
        FamilyMembership.objects.create(user=child, family=self.family, role='enfants')
        self.client.force_login(child)
        resp = self.client.get(reverse('today'), {'who': 'fils'})
        self.assertIsNone(resp.context['who_options'])
        self.assertEqual(sorted(c['person'] for c in resp.context['kid_cards']), ['fille', 'fils'])


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

class SharedEventSelectionTests(TestCase):
    """task_logic.occurs_on / activities_on : une seule règle de sélection, partagée par tous
    les écrans. Un événement ponctuel n'existe qu'à sa date et ne se répète jamais."""

    def setUp(self):
        self.family = Family.objects.create(name='Ev', invite_code='EVENTFAM1')
        self.settings = FamilySettings.load(self.family)
        self.monday = _real_date_for_day('lundi')
        self.next_monday = self.monday + datetime.timedelta(days=7)

    def _activity(self, **kw):
        kw.setdefault('person', 'fille')
        kw.setdefault('label', 'Natation')
        kw.setdefault('day', 'lundi')
        return Activity.objects.create(family=self.family, **kw)

    def test_recurring_activity_happens_every_matching_weekday(self):
        act = self._activity()
        self.assertTrue(occurs_on(act, self.monday))
        self.assertTrue(occurs_on(act, self.next_monday))
        self.assertFalse(occurs_on(act, self.monday + datetime.timedelta(days=1)))

    def test_one_off_happens_only_on_its_date_never_weekly(self):
        act = self._activity(specific_date=self.monday)
        self.assertTrue(occurs_on(act, self.monday))
        # Même jour de la semaine, semaine suivante : ne doit PAS réapparaître.
        self.assertFalse(occurs_on(act, self.next_monday))

    def test_activities_on_mixes_recurring_and_one_off_for_that_date(self):
        recurring = self._activity(label='Piano')
        one_off = self._activity(label='Dentiste', specific_date=self.monday, day='jeudi')
        today_ids = {a.id for a in activities_on(list(Activity.objects.all()), self.monday)}
        self.assertEqual(today_ids, {recurring.id, one_off.id})
        later_ids = {a.id for a in activities_on(list(Activity.objects.all()), self.next_monday)}
        self.assertEqual(later_ids, {recurring.id})

    def test_one_off_absent_from_generated_tasks_on_other_weeks(self):
        act = self._activity(label='Dentiste', specific_date=self.monday, start_time=datetime.time(10, 0))
        acts = list(Activity.objects.all())
        ids_that_day = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, acts, date=self.monday)}
        ids_next_week = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, acts, date=self.next_monday)}
        self.assertIn(f'activite_{act.id}', ids_that_day)
        self.assertNotIn(f'activite_{act.id}', ids_next_week)

    def test_week_view_shows_a_one_off_only_on_its_date(self):
        parent = User.objects.create_user('evparent', password='pass12345')
        FamilyMembership.objects.create(user=parent, family=self.family, role='maman')
        self.client.force_login(parent)
        self._activity(label='Dentiste', specific_date=self.monday, day='lundi')
        this_week = self.client.get(reverse('week'), {'week': _monday_of(self.monday).isoformat()})
        next_week = self.client.get(reverse('week'), {'week': _monday_of(self.next_monday).isoformat()})
        self.assertContains(this_week, 'Dentiste')
        self.assertNotContains(next_week, 'Dentiste')


class ActivityTaskShapeTests(TestCase):
    """Les tâches issues d'une activité : identifiant stable (pk, pas la position) et phase
    déduite de l'heure de début plutôt que « soir » systématique."""

    def setUp(self):
        self.family = Family.objects.create(name='Sh', invite_code='SHAPEFAM1')
        self.settings = FamilySettings.load(self.family)
        self.monday = _real_date_for_day('lundi')

    def test_task_id_follows_the_activity_pk_not_its_position(self):
        first = Activity.objects.create(family=self.family, person='fille', label='A', day='lundi')
        second = Activity.objects.create(family=self.family, person='fille', label='B', day='lundi')
        acts = [first, second]
        ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, acts, date=self.monday)}
        self.assertIn(f'activite_{first.id}', ids)
        self.assertIn(f'activite_{second.id}', ids)

        # La première disparaît : l'identifiant de la seconde ne bouge pas.
        ids_after = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [second], date=self.monday)}
        self.assertIn(f'activite_{second.id}', ids_after)
        self.assertNotIn(f'activite_{first.id}', ids_after)

    def test_morning_activity_is_not_filed_under_the_evening_routine(self):
        morning = Activity.objects.create(family=self.family, person='fille', label='Piscine',
                                          day='lundi', start_time=datetime.time(9, 0))
        evening = Activity.objects.create(family=self.family, person='fille', label='Judo',
                                          day='lundi', start_time=datetime.time(19, 0))
        tasks = tasks_for('fille', 'lundi', self.settings, [morning, evening], date=self.monday)
        by_id = {t['id']: t for t in tasks}
        self.assertEqual(by_id[f'activite_{morning.id}']['period'], 'matin')
        self.assertEqual(by_id[f'activite_{evening.id}']['period'], 'soir')

    def test_phase_for_time_cutoffs(self):
        self.assertEqual(phase_for_time(datetime.time(8, 0)), 'matin')
        self.assertEqual(phase_for_time(datetime.time(12, 0)), 'journee')
        self.assertEqual(phase_for_time(datetime.time(17, 59)), 'journee')
        self.assertEqual(phase_for_time(datetime.time(18, 0)), 'soir')
        self.assertEqual(phase_for_time(None), 'soir')


class TripAssignmentTests(TestCase):
    """Les trajets vont à l'adulte réellement désigné (accompanied_by / picked_up_by), plus
    systématiquement au père ; sans personne désignée, aucune tâche n'est inventée."""

    def setUp(self):
        self.family = Family.objects.create(name='Tr', invite_code='TRIPFAM1')
        self.settings = FamilySettings.load(self.family)
        self.settings.papa_travaille = True
        self.settings.save()
        self.monday = _real_date_for_day('lundi')

    def _tasks(self, person):
        acts = list(Activity.objects.filter(family=self.family))
        return {t['id'] for t in tasks_for(person, 'lundi', self.settings, acts, date=self.monday)}

    def test_drop_off_goes_to_the_designated_parent(self):
        act = Activity.objects.create(
            family=self.family, person='fille', label='Natation', day='lundi',
            start_time=datetime.time(17, 0), accompanied_by='maman',
        )
        self.assertIn(f'drive_{act.id}', self._tasks('maman'))
        self.assertNotIn(f'drive_{act.id}', self._tasks('papa'))

    def test_pick_up_can_be_a_different_parent_than_the_drop_off(self):
        act = Activity.objects.create(
            family=self.family, person='fils', label='Judo', day='lundi',
            start_time=datetime.time(17, 0), end_time=datetime.time(18, 0),
            accompanied_by='maman', picked_up_by='papa',
        )
        self.assertIn(f'drive_{act.id}', self._tasks('maman'))
        self.assertNotIn(f'pickup_{act.id}', self._tasks('maman'))
        self.assertIn(f'pickup_{act.id}', self._tasks('papa'))

    def test_activity_without_a_designated_adult_creates_no_trip_task(self):
        act = Activity.objects.create(
            family=self.family, person='fille', label='Danse', day='lundi',
            start_time=datetime.time(17, 0),
        )
        for person in ('maman', 'papa'):
            self.assertNotIn(f'drive_{act.id}', self._tasks(person))
            self.assertNotIn(f'pickup_{act.id}', self._tasks(person))

    def test_no_duplicate_trip_task_for_the_same_activity(self):
        act = Activity.objects.create(
            family=self.family, person='fille', label='Natation', day='lundi',
            start_time=datetime.time(17, 0), accompanied_by='papa',
        )
        acts = list(Activity.objects.filter(family=self.family))
        ids = [t['id'] for t in tasks_for('papa', 'lundi', self.settings, acts, date=self.monday)]
        self.assertEqual(ids.count(f'drive_{act.id}'), 1)


class WeekContextPropagationTests(TestCase):
    """La semaine choisie sur le semainier doit suivre sur les menus, les courses et la
    préparation de semaine — et surtout ne jamais faire écrire dans la semaine courante par
    accident après une soumission de formulaire."""

    def setUp(self):
        self.family = Family.objects.create(name='Wk', invite_code='WEEKCTXFAM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('wkparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)
        self.this_monday = _monday_of(datetime.date.today())
        self.next_monday = self.this_monday + datetime.timedelta(days=7)
        self.recipe = Recipe.objects.create(family=self.family, name='Soupe', category='Soupe',
                                            ingredients=[{'name': 'Carotte', 'quantity': 200, 'unit': 'g'}])

    def test_menu_reads_the_requested_week(self):
        WeeklyMenuEntry.objects.create(family=self.family, week_start=self.next_monday,
                                       day='lundi', recipe=self.recipe)
        resp = self.client.get(reverse('menu'), {'week': self.next_monday.isoformat()})
        self.assertEqual(resp.context['week_start'], self.next_monday)
        self.assertFalse(resp.context['is_current_week'])
        selected = {r['day']: r['selected'] for r in resp.context['day_rows']}
        self.assertEqual(selected['lundi'], self.recipe.id)

    def test_setting_a_meal_writes_to_the_chosen_week_not_the_current_one(self):
        resp = self.client.post(
            f"{reverse('menu')}?week={self.next_monday.isoformat()}",
            {'set_day': 'mardi', 'recipe_id': self.recipe.id, 'week': self.next_monday.isoformat()},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(WeeklyMenuEntry.objects.filter(
            family=self.family, week_start=self.next_monday, day='mardi', recipe=self.recipe).exists())
        self.assertFalse(WeeklyMenuEntry.objects.filter(
            family=self.family, week_start=self.this_monday, day='mardi').exists())

    def test_redirect_after_submission_keeps_the_week(self):
        resp = self.client.post(
            f"{reverse('menu')}?week={self.next_monday.isoformat()}",
            {'set_day': 'mardi', 'recipe_id': self.recipe.id, 'week': self.next_monday.isoformat()},
        )
        self.assertIn(f'week={self.next_monday.isoformat()}', resp['Location'])

    def test_copy_to_courses_tags_items_with_the_prepared_week(self):
        WeeklyMenuEntry.objects.create(family=self.family, week_start=self.next_monday,
                                       day='lundi', recipe=self.recipe)
        self.client.post(
            f"{reverse('menu')}?week={self.next_monday.isoformat()}",
            {'copy_to_courses': '1', 'week': self.next_monday.isoformat()},
        )
        item = GroceryItem.objects.get(family=self.family, name='Carotte')
        self.assertEqual(item.week_start, self.next_monday)

    def test_courses_of_another_week_are_not_shown_nor_overwritten(self):
        other = GroceryItem.objects.create(family=self.family, name='Poireau',
                                           week_start=self.this_monday, category='Menu de la semaine')
        manual = GroceryItem.objects.create(family=self.family, name='Éponges', category='Ajoutés')
        resp = self.client.get(reverse('maison'), {'week': self.next_monday.isoformat()})
        shown = {i.name for items in resp.context['grouped'].values() for i in items}
        self.assertNotIn('Poireau', shown)        # article d'une autre semaine : mis de côté
        self.assertIn('Éponges', shown)           # ajout manuel : valable quelle que soit la semaine
        self.assertEqual(resp.context['other_week_count'], 1)
        other.refresh_from_db()
        self.assertEqual(other.week_start, self.this_monday)   # jamais réécrit

    def test_adding_an_item_from_a_prepared_week_keeps_that_week_in_the_redirect(self):
        resp = self.client.post(reverse('add_grocery'),
                                {'name': 'Levure', 'week': self.next_monday.isoformat()})
        self.assertIn(f'week={self.next_monday.isoformat()}', resp['Location'])
        # Un ajout manuel n'appartient à aucune semaine : il reste visible partout.
        self.assertIsNone(GroceryItem.objects.get(family=self.family, name='Levure').week_start)

    def test_invalid_week_param_falls_back_to_the_current_week(self):
        resp = self.client.get(reverse('menu'), {'week': 'pas-une-date'})
        self.assertEqual(resp.context['week_start'], self.this_monday)
        self.assertTrue(resp.context['is_current_week'])

    def test_semainier_links_to_the_same_week_on_menu_and_courses(self):
        resp = self.client.get(reverse('week'), {'week': self.next_monday.isoformat()})
        html = resp.content.decode()
        self.assertIn(f"{reverse('menu')}?week={self.next_monday.isoformat()}", html)
        self.assertIn(f"{reverse('maison')}?week={self.next_monday.isoformat()}", html)


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

    def test_coran_is_never_dropped_by_any_day_mode(self):
        for mode in ('normal', 'vacances', 'absence', 'allegee'):
            with self.subTest(day_mode=mode):
                ids = {t['id'] for t in tasks_for('fille', 'lundi', self.settings, [], day_mode=mode)}
                self.assertIn('coran', ids)

    def test_allegee_shortens_coran_instead_of_dropping_it(self):
        normal_coran = next(t for t in tasks_for('fille', 'lundi', self.settings, []) if t['id'] == 'coran')
        allegee_coran = next(
            t for t in tasks_for('fille', 'lundi', self.settings, [], day_mode='allegee') if t['id'] == 'coran'
        )
        absence_coran = next(
            t for t in tasks_for('fille', 'lundi', self.settings, [], day_mode='absence') if t['id'] == 'coran'
        )
        self.assertNotEqual(allegee_coran['label'], normal_coran['label'])
        self.assertEqual(absence_coran['label'], normal_coran['label'])


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


class DayPickerTests(TestCase):
    """The day picker (_day_picker.html): one submit stores one recurring task, the school-day
    shortcut reads task_logic.SCHOOL_DAYS rather than a hardcoded list, editing prefills the
    right boxes, and an empty selection is refused with a message that says which half is
    missing."""

    def setUp(self):
        self.family = Family.objects.create(name='DP', invite_code='DAYPICKFAM1')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('dpparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)

    def _messages(self, resp):
        return [str(m) for m in get_messages(resp.wsgi_request)]

    def test_selecting_all_seven_days_creates_one_task_not_seven(self):
        resp = self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fille', 'task_label': 'Quotidienne',
            'task_days': list(DAYS), 'task_period': 'matin',
        })
        self.assertEqual(resp.status_code, 302)
        tasks = CustomTask.objects.filter(family=self.family, label='Quotidienne')
        self.assertEqual(tasks.count(), 1)
        self.assertEqual(sorted(tasks.first().days), sorted(DAYS))
        self.assertIn('tous les jours', self._messages(resp)[-1])

    def test_school_days_shortcut_matches_task_logic_config(self):
        resp = self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fils', 'task_label': 'Cartable',
            'task_days': list(SCHOOL_DAYS), 'task_period': 'soir',
        })
        task = CustomTask.objects.get(family=self.family, label='Cartable')
        self.assertEqual(sorted(task.days), sorted(SCHOOL_DAYS))
        self.assertNotIn('mercredi', task.days)  # mercredi n'est pas un jour d'école ici
        self.assertIn("les jours d'école", self._messages(resp)[-1])

    def test_weekend_shortcut_selects_saturday_and_sunday(self):
        self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fille', 'task_label': 'Grasse mat',
            'task_days': list(WEEKEND_DAYS), 'task_period': 'matin',
        })
        task = CustomTask.objects.get(family=self.family, label='Grasse mat')
        self.assertEqual(sorted(task.days), ['dimanche', 'samedi'])

    def test_picker_renders_a_checkbox_per_day_with_shortcut_config(self):
        resp = self.client.get(reverse('settings'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for day in DAYS:
            self.assertIn(f'name="task_days" value="{day}"', html)
        self.assertIn(f'data-school-days="{",".join(SCHOOL_DAYS)}"', html)
        self.assertIn(f'data-weekend-days="{",".join(WEEKEND_DAYS)}"', html)

    def test_edit_form_prefills_the_tasks_current_days(self):
        CustomTask.objects.create(
            family=self.family, person='fille', days=['mardi', 'jeudi'], period='soir', label='Danse',
        )
        html = self.client.get(reverse('settings')).content.decode()
        checked = {
            m.group(1) for m in
            re.finditer(r'name="task_days" value="(\w+)"\s+checked', html)
        }
        # Seul le formulaire de modification préremplit : le formulaire d'ajout reste vierge.
        self.assertEqual(checked, {'mardi', 'jeudi'})

    def test_empty_selection_is_refused_with_an_explicit_message(self):
        resp = self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fille', 'task_label': 'SansJour', 'task_period': 'matin',
        })
        self.assertFalse(CustomTask.objects.filter(family=self.family, label='SansJour').exists())
        self.assertIn('au moins un jour', self._messages(resp)[-1])

    def test_missing_label_message_names_the_label_not_the_days(self):
        resp = self.client.post(reverse('settings'), {
            'add_custom_task': '1', 'task_person': 'fille', 'task_days': ['lundi'], 'task_period': 'matin',
        })
        message = self._messages(resp)[-1]
        self.assertIn('intitulé', message)
        self.assertNotIn('au moins un jour', message)

    def test_editing_to_an_empty_selection_keeps_the_previous_days(self):
        task = CustomTask.objects.create(
            family=self.family, person='fille', days=['lundi'], period='matin', label='Garder',
        )
        self.client.post(reverse('edit_custom_task', args=[task.id]), {
            'task_person': 'fille', 'task_label': 'Garder', 'task_period': 'matin',
        })
        task.refresh_from_db()
        self.assertEqual(task.days, ['lundi'])


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


class WizardEntryTests(TestCase):
    """wizard_start (Lot 5b): the intro/"Commencer" card and the "Terminé !" recap card are
    the same view, switched by ?done=1 — and it's parent-only since step 2 of the flow lands
    on settings_view (parent_required)."""

    def setUp(self):
        self.family = Family.objects.create(name='Wiz', invite_code='WIZFAM001')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('wizparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.child = User.objects.create_user('wizchild', password='pass12345')
        FamilyMembership.objects.create(user=self.child, family=self.family, role='enfants')

    def test_child_cannot_access_wizard_start(self):
        self.client.force_login(self.child)
        resp = self.client.get(reverse('wizard_start'))
        self.assertEqual(resp.status_code, 403)

    def test_parent_sees_intro_card_with_link_to_step_one(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('wizard_start'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Commencer')
        self.assertContains(resp, f"{reverse('week')}?wizard=1&amp;step=1")

    def test_parent_sees_recap_card_when_done(self):
        self.client.force_login(self.parent)
        resp = self.client.get(reverse('wizard_start'), {'done': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Semaine prête')
        self.assertContains(resp, reverse('week'))
        self.assertContains(resp, reverse('maison'))


class WizardBannerDisplayTests(TestCase):
    """The 4 existing screens (week_view, settings_view, menu, maison) only show the wizard
    progress banner when ?wizard=1 is on the URL (see views._wizard_banner) — off by default,
    so nothing changes for a family that never uses the "Préparer notre semaine" entry point."""

    def setUp(self):
        self.family = Family.objects.create(name='WizB', invite_code='WIZBFAM01')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('wizbparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)

    def test_week_view_has_no_banner_without_wizard_param(self):
        resp = self.client.get(reverse('week'))
        self.assertNotContains(resp, 'wizard-banner')

    def test_week_view_step_one_banner_points_to_settings_step_two(self):
        resp = self.client.get(reverse('week'), {'wizard': '1', 'step': '1'})
        self.assertContains(resp, 'wizard-banner')
        self.assertContains(resp, 'Étape 1/4')
        self.assertContains(resp, f"{reverse('settings')}?wizard=1&amp;step=2")

    def test_settings_view_step_two_banner_points_to_menu_step_three(self):
        resp = self.client.get(reverse('settings'), {'wizard': '1', 'step': '2'})
        self.assertContains(resp, 'wizard-banner')
        self.assertContains(resp, 'Étape 2/4')
        self.assertContains(resp, f"{reverse('menu')}?wizard=1&amp;step=3")

    def test_menu_step_three_banner_points_to_menu_step_four(self):
        resp = self.client.get(reverse('menu'), {'wizard': '1', 'step': '3'})
        self.assertContains(resp, 'wizard-banner')
        self.assertContains(resp, 'Étape 3/4')
        self.assertContains(resp, f"{reverse('menu')}?wizard=1&amp;step=4")

    def test_menu_step_four_banner_points_to_maison_step_four(self):
        resp = self.client.get(reverse('menu'), {'wizard': '1', 'step': '4'})
        self.assertContains(resp, 'wizard-banner')
        self.assertContains(resp, 'Étape 4/4')
        self.assertContains(resp, f"{reverse('maison')}?wizard=1&amp;step=4")

    def test_maison_step_four_is_the_last_step_with_a_finish_button(self):
        resp = self.client.get(reverse('maison'), {'wizard': '1', 'step': '4'})
        self.assertContains(resp, 'wizard-banner')
        self.assertContains(resp, 'Étape 4/4')
        self.assertContains(resp, 'Terminer le parcours')
        self.assertNotContains(resp, 'Étape suivante')

    def test_maison_has_no_banner_without_wizard_param(self):
        resp = self.client.get(reverse('maison'))
        self.assertNotContains(resp, 'wizard-banner')


class WizardCopyToCoursesHandoffTests(TestCase):
    """Step 4 of the wizard is the existing copy_to_courses action itself (see task brief) —
    when triggered from inside the flow it hands off straight to 'maison' instead of looping
    back to 'menu', and normal (non-wizard) usage is unaffected."""

    def setUp(self):
        self.family = Family.objects.create(name='WizC', invite_code='WIZCFAM01')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('wizcparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)
        recipe = Recipe.objects.create(
            family=self.family, name='Pâtes', category='Autre',
            ingredients=[{'name': 'Pâtes', 'quantity': 250, 'unit': 'g'}],
        )
        week_start = _monday_of(datetime.date.today())
        WeeklyMenuEntry.objects.create(family=self.family, week_start=week_start, day='lundi', recipe=recipe)

    def test_copy_to_courses_inside_wizard_redirects_to_maison_step_four(self):
        resp = self.client.post(
            f"{reverse('menu')}?wizard=1&step=4", {'copy_to_courses': '1'}
        )
        # La redirection porte aussi la semaine préparée (WeekContextPropagationTests) : on
        # vérifie donc la destination et les paramètres qui comptent, pas la chaîne exacte.
        path, _, query = resp['Location'].partition('?')
        self.assertEqual(path, reverse('maison'))
        self.assertIn('wizard=1', query)
        self.assertIn('step=4', query)
        self.assertTrue(GroceryItem.objects.filter(family=self.family, name='Pâtes').exists())

    def test_copy_to_courses_outside_wizard_still_redirects_to_menu(self):
        resp = self.client.post(reverse('menu'), {'copy_to_courses': '1'})
        path, _, query = resp['Location'].partition('?')
        self.assertEqual(path, reverse('menu'))
        self.assertNotIn('wizard', query)
        self.assertNotIn('step', query)


class WizardStepPreservedAcrossSamePageActionsTests(TestCase):
    """_wizard_redirect: same-page actions (picking a recipe for a day, saving settings...)
    must not silently drop the wizard out from under the user after their first click —
    without this, the banner would vanish the moment they did anything on the step."""

    def setUp(self):
        self.family = Family.objects.create(name='WizD', invite_code='WIZDFAM01')
        FamilySettings.load(self.family)
        self.parent = User.objects.create_user('wizdparent', password='pass12345')
        FamilyMembership.objects.create(user=self.parent, family=self.family, role='maman')
        self.client.force_login(self.parent)
        self.recipe = Recipe.objects.create(family=self.family, name='Riz cantonais', category='Autre')

    def test_set_day_inside_wizard_redirects_back_with_step_kept(self):
        resp = self.client.post(f"{reverse('menu')}?wizard=1&step=3", {
            'set_day': '1', 'day': 'lundi', 'recipe_id': self.recipe.id,
        })
        path, _, query = resp['Location'].partition('?')
        self.assertEqual(path, reverse('menu'))
        self.assertIn('wizard=1', query)
        self.assertIn('step=3', query)

    def test_set_day_outside_wizard_redirects_without_wizard_params(self):
        resp = self.client.post(reverse('menu'), {
            'set_day': '1', 'day': 'lundi', 'recipe_id': self.recipe.id,
        })
        path, _, query = resp['Location'].partition('?')
        self.assertEqual(path, reverse('menu'))
        self.assertNotIn('wizard', query)
        self.assertNotIn('step', query)

    def test_settings_save_inside_wizard_redirects_back_with_step_kept(self):
        resp = self.client.post(f"{reverse('settings')}?wizard=1&step=2", {
            'maman_name': 'Maman', 'nb_enfants': '1', 'fille_name': 'Léa',
            'tt2_day': 'lundi', 'courses_day': 'samedi',
        })
        self.assertRedirects(resp, f"{reverse('settings')}?wizard=1&step=2")
