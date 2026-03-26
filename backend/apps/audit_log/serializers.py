from rest_framework import serializers

from .models import AuditRecord


class AuditRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditRecord
        fields = ["id", "session", "event_type", "actor", "payload", "created_at"]
        read_only_fields = fields
