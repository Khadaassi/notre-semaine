import datetime
import importlib
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import RecipeForm, parse_ingredients_text
from .models import (
    Family, FamilySettings, FamilyMembership, PARENT_ROLES, StarAward, KidStars,
    TaskCompletion, TaskException, Activity, Recipe, GroceryItem, WeeklyMenuEntry,
)
from .task_logic import is_zone_b_holiday, ZONE_B_HOLIDAYS, DAYS, tasks_for
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
