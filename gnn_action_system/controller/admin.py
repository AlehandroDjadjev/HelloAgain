from django.contrib import admin
from .models import Action, ActionAttributeScore, Attribute, MainUserProfile, UserActionEdge, UserAttributeScore

admin.site.register(Attribute)
admin.site.register(MainUserProfile)
admin.site.register(UserAttributeScore)
admin.site.register(Action)
admin.site.register(ActionAttributeScore)
admin.site.register(UserActionEdge)
