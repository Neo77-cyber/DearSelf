# habits/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("verify-otp/", views.verify_otp_view, name="verify_otp"),
    path("resend-otp/", views.resend_otp, name="resend_otp"),
    path("login/", views.login_view, name="login"),
    path("login/code/", views.login_verify, name="login_verify"),
    path("login/link/<str:token>/", views.login_link_view, name="login_link"),
    path("telegram/webhook/", views.telegram_webhook, name="telegram_webhook"),
    path("logout/", views.user_logout, name="logout"),
    path("habits/", views.habit_list, name="habit_list"),
    path("habit/add/", views.add_habit, name="add_habit"),
    path(
        "habits/<int:habit_id>/mark-done/",
        views.mark_habit_done,
        name="mark_habit_done",
    ),
    path("c/<str:token>/", views.clock_in, name="clock_in"),
    path("banned/", views.banned_view, name="banned"),
    path("maintenance_trigger/", views.maintenance_trigger, name="maintenance_check"),
    path("health/", views.health_check, name="health_check"),
]
