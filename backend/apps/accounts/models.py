import uuid
from django.db import models

class ElderlyUser(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    egn = models.CharField(max_length=10, unique=True, help_text="Bulgarian national ID")
    date_of_birth = models.DateField()
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    home_lat = models.FloatField(blank=True, null=True, help_text="Default home latitude")
    home_lng = models.FloatField(blank=True, null=True, help_text="Default home longitude")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.egn})"
