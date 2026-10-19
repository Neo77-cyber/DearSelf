from django.contrib import admin
from .models import Habit, Profile, DailyRecord, ServiceFlag
from auditlog.registry import auditlog

# Register your models here.
auditlog.register(Habit)
admin.site.register(Habit)
admin.site.register(Profile)
admin.site.register(DailyRecord)
admin.site.register(ServiceFlag)
