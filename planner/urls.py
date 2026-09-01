from django.urls import path
from . import views

urlpatterns = [
    path('', views.today, name='today'),
    path('toggle-task/', views.toggle_task, name='toggle_task'),
    path('semaine/', views.week_view, name='week'),
    path('maison/', views.maison, name='maison'),
    path('maison/toggle-course/', views.toggle_grocery, name='toggle_grocery'),
    path('maison/add-course/', views.add_grocery, name='add_grocery'),
    path('maison/reset-courses/', views.reset_grocery, name='reset_grocery'),
    path('maison/toggle-rotation/', views.toggle_rotation, name='toggle_rotation'),
    path('menu/', views.menu, name='menu'),
    path('reglages/', views.settings_view, name='settings'),
    path('reglages/activite/<int:pk>/supprimer/', views.delete_activity, name='delete_activity'),
    path('inscription/', views.signup, name='signup'),
]
