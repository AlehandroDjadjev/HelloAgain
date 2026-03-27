from django.db import models


class MainUserProfile(models.Model):
    name = models.CharField(max_length=120, default="main_user", unique=True)
    description = models.TextField(blank=True, default="")
    state_history = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Attribute(models.Model):
    name = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class UserAttributeScore(models.Model):
    user = models.ForeignKey(MainUserProfile, on_delete=models.CASCADE, related_name="attribute_scores")
    attribute = models.ForeignKey(Attribute, on_delete=models.CASCADE, related_name="user_scores")
    score = models.FloatField(default=0.0)
    confidence = models.FloatField(default=1.0)
    history_stack = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "attribute")

    def __str__(self) -> str:
        return f"{self.user.name} -> {self.attribute.name}: {self.score:.3f}"


class Action(models.Model):
    name = models.CharField(max_length=160, unique=True)
    description = models.TextField(blank=True, default="")
    base_summary = models.TextField(blank=True, default="")
    created_from_prompt = models.TextField(blank=True, default="")
    desired_attribute_map = models.JSONField(default=dict, blank=True)
    history_stack = models.JSONField(default=list, blank=True)
    hit_count = models.IntegerField(default=0)
    helpful_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class ActionAttributeScore(models.Model):
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="attribute_scores")
    attribute = models.ForeignKey(Attribute, on_delete=models.CASCADE, related_name="action_scores")
    score = models.FloatField(default=0.0)
    contribution_count = models.IntegerField(default=0)
    history_stack = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("action", "attribute")

    def __str__(self) -> str:
        return f"{self.action.name} -> {self.attribute.name}: {self.score:.3f}"


class UserActionEdge(models.Model):
    SIGNAL_CHOICES = [
        ("neutral", "neutral"),
        ("desire", "desire"),
        ("positive", "positive"),
        ("negative", "negative"),
        ("memory", "memory"),
        ("fetch", "fetch"),
    ]

    user = models.ForeignKey(MainUserProfile, on_delete=models.CASCADE, related_name="action_edges")
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="user_edges")
    score = models.FloatField(default=0.0)
    confidence = models.FloatField(default=1.0)
    last_signal_kind = models.CharField(max_length=32, choices=SIGNAL_CHOICES, default="neutral")
    touch_count = models.IntegerField(default=0)
    signal_history = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "action")

    def __str__(self) -> str:
        return f"{self.user.name} -> {self.action.name}: {self.score:.3f}"
