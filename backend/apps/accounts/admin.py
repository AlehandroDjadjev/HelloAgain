from django.contrib import admin
from .models import ElderlyUser

@admin.register(ElderlyUser)
class ElderlyUserAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'egn', 'phone', 'created_at')
    search_fields = ('first_name', 'last_name', 'egn', 'phone')
    readonly_fields = ('created_at', 'updated_at', 'id')
