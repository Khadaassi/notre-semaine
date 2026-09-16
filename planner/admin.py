from django.contrib import admin
from .models import (
    Family, FamilyMembership, FamilySettings, Activity, TaskCompletion, Recipe,
    WeeklyMenuEntry, CustomTask, GroceryItem, TaskOrder, StarAward, KidStars, TaskException,
    DayMode,
)


class FamilyMembershipInline(admin.TabularInline):
    model = FamilyMembership
    extra = 0


class FamilyAdmin(admin.ModelAdmin):
    list_display = ('name', 'invite_code', 'created_at')
    inlines = [FamilyMembershipInline]


admin.site.register(Family, FamilyAdmin)
admin.site.register(FamilyMembership)
admin.site.register(FamilySettings)
admin.site.register(Activity)
admin.site.register(TaskCompletion)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'is_favorite', 'duration_minutes', 'family')
    list_filter = ('category', 'is_favorite', 'family')


class GroceryItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'checked', 'already_home', 'is_default', 'family')
    list_filter = ('category', 'checked', 'already_home', 'is_default', 'family')


admin.site.register(Recipe, RecipeAdmin)
admin.site.register(WeeklyMenuEntry)
admin.site.register(CustomTask)
admin.site.register(GroceryItem, GroceryItemAdmin)
admin.site.register(TaskOrder)
admin.site.register(StarAward)
admin.site.register(KidStars)
admin.site.register(TaskException)
admin.site.register(DayMode)
