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
admin.site.register(Recipe)
admin.site.register(WeeklyMenuEntry)
admin.site.register(CustomTask)
admin.site.register(GroceryItem)
admin.site.register(TaskOrder)
admin.site.register(StarAward)
admin.site.register(KidStars)
admin.site.register(TaskException)
admin.site.register(DayMode)
