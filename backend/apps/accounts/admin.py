from django.contrib import admin
from .models import AccountProfile, AccountToken, ElderlyUser, FriendRequest, ImportedContact

@admin.register(ElderlyUser)
class ElderlyUserAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'egn', 'phone', 'created_at')
    search_fields = ('first_name', 'last_name', 'egn', 'phone')
    readonly_fields = ('created_at', 'updated_at', 'id')


@admin.register(AccountProfile)
class AccountProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "display_name",
        "phone_number",
        "contacts_permission_granted",
        "elder_profile",
        "created_at",
    )
    search_fields = ("user__username", "user__email", "display_name", "phone_number")
    readonly_fields = ("created_at", "updated_at", "contacts_permission_granted_at")


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ("from_profile", "to_profile", "status", "created_at", "responded_at")
    list_filter = ("status",)
    search_fields = ("from_profile__display_name", "to_profile__display_name")
    readonly_fields = ("created_at", "updated_at", "responded_at")


@admin.register(AccountToken)
class AccountTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "key", "created_at", "last_used_at")
    search_fields = ("user__username", "user__email", "key")
    readonly_fields = ("created_at", "last_used_at")


@admin.register(ImportedContact)
class ImportedContactAdmin(admin.ModelAdmin):
    list_display = ("full_name", "owner", "phone_number", "email", "source", "created_at")
    search_fields = ("full_name", "owner__display_name", "phone_number", "email")
    readonly_fields = ("created_at", "updated_at")
