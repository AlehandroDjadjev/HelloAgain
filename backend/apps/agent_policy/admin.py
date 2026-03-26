from django.contrib import admin

from .models import PolicyDecisionRecord, SystemPolicyConfig, UserAutomationPolicy


@admin.register(UserAutomationPolicy)
class UserAutomationPolicyAdmin(admin.ModelAdmin):
    list_display = [
        "user_id", "org_id",
        "allow_text_entry", "allow_send_actions",
        "require_hard_confirmation_for_send",
        "max_steps_per_plan", "risk_threshold",
        "updated_at",
    ]
    list_filter  = ["risk_threshold", "allow_text_entry", "allow_send_actions"]
    search_fields = ["user_id", "org_id"]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = [
        ("Identity", {"fields": ["user_id", "org_id"]}),
        ("Package Access", {"fields": ["allowed_packages"]}),
        ("Action Controls", {"fields": [
            "blocked_action_types",
            "always_confirm_action_types",
            "allow_text_entry",
            "allow_send_actions",
            "require_hard_confirmation_for_send",
        ]}),
        ("Keywords", {"fields": ["blocked_keywords"]}),
        ("Risk & Safety", {"fields": [
            "max_steps_per_plan",
            "sensitive_screen_policy",
            "risk_threshold",
            "allow_coordinates_fallback",
        ]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"]}),
    ]


@admin.register(SystemPolicyConfig)
class SystemPolicyConfigAdmin(admin.ModelAdmin):
    list_display  = ["max_plan_length", "updated_at", "updated_by"]
    readonly_fields = ["updated_at"]

    def has_add_permission(self, request):
        return not SystemPolicyConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False   # Prevent accidental deletion of the singleton


@admin.register(PolicyDecisionRecord)
class PolicyDecisionRecordAdmin(admin.ModelAdmin):
    list_display  = ["rule_name", "decision", "action_id", "session_id", "created_at"]
    list_filter   = ["decision", "rule_name"]
    search_fields = ["session__id", "plan_id", "rule_name", "reason"]
    readonly_fields = [
        "session", "plan_id", "rule_name", "action_id",
        "decision", "reason", "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
