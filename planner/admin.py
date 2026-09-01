from django.contrib import admin
from .models import FamilySettings, Activity, TaskCompletion, Recipe, WeeklyMenuEntry, GroceryItem

admin.site.register(FamilySettings)
admin.site.register(Activity)
admin.site.register(TaskCompletion)
admin.site.register(Recipe)
admin.site.register(WeeklyMenuEntry)
admin.site.register(GroceryItem)
