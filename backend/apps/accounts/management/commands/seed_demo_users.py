from __future__ import annotations

import secrets

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import AccountProfile, normalize_phone_number
from apps.accounts.services import (
    build_voice_username,
    seed_social_graph_for_profile,
    sync_profile_to_recommendations,
)


DEMO_USERS = [
    {
        "name": "Mila Petrova",
        "phone": "+359 887 100 001",
        "description": "Warm, observant, and loves slow coffee conversations, sketching, and weekend gallery walks.",
    },
    {
        "name": "Georgi Marinov",
        "phone": "+359 887 100 002",
        "description": "Energetic and playful, always up for volleyball, road trips, and organizing group outings.",
    },
    {
        "name": "Nadia Koleva",
        "phone": "+359 887 100 003",
        "description": "Reflective and bookish, happiest with poetry, quiet parks, and long thoughtful chats.",
    },
    {
        "name": "Petar Iliev",
        "phone": "+359 887 100 004",
        "description": "Curious tech tinkerer who enjoys indie games, hardware projects, and clever late-night jokes.",
    },
    {
        "name": "Raya Stoyanova",
        "phone": "+359 887 100 005",
        "description": "Gentle and artistic, into ceramics, plant care, and cozy dinners with a few close people.",
    },
    {
        "name": "Viktor Dimitrov",
        "phone": "+359 887 100 006",
        "description": "Sporty, direct, and upbeat; likes hiking, football, and motivating friends to get outside.",
    },
    {
        "name": "Elena Todorova",
        "phone": "+359 887 100 007",
        "description": "Empathetic listener who loves baking, family stories, and helping shy people feel included.",
    },
    {
        "name": "Asen Nikolov",
        "phone": "+359 887 100 008",
        "description": "Dry sense of humor, into chess, documentaries, and calm conversations about ideas.",
    },
    {
        "name": "Borislava Ivanova",
        "phone": "+359 887 100 009",
        "description": "Social and bright, enjoys dance classes, brunches, and checking out new places in the city.",
    },
    {
        "name": "Daniela Hristova",
        "phone": "+359 887 100 010",
        "description": "Soft-spoken and dependable, prefers one-to-one friendships, podcasts, and evening walks.",
    },
    {
        "name": "Kalin Vasilev",
        "phone": "+359 887 100 011",
        "description": "Adventure-driven and competitive, into climbing, travel planning, and spontaneous meetups.",
    },
    {
        "name": "Yoana Popova",
        "phone": "+359 887 100 012",
        "description": "Creative and funny, loves fashion, photos, cafés, and talking for hours with the right person.",
    },
    {
        "name": "Stanislav Rusev",
        "phone": "+359 887 100 013",
        "description": "Steady and practical, happiest around woodworking, DIY repairs, and honest conversation.",
    },
    {
        "name": "Monika Angelova",
        "phone": "+359 887 100 014",
        "description": "Sensitive and romantic, loves flowers, piano covers, handwritten notes, and meaningful connection.",
    },
    {
        "name": "Ivo Ganchev",
        "phone": "+359 887 100 015",
        "description": "Laid-back and loyal, into comedy, basketball, and easygoing chats without pressure.",
    },
    {
        "name": "Teodora Savova",
        "phone": "+359 887 100 016",
        "description": "Organized and nurturing, enjoys yoga, healthy cooking, and supportive friendships.",
    },
    {
        "name": "Kristian Bonev",
        "phone": "+359 887 100 017",
        "description": "Music-first extrovert who likes live gigs, jam sessions, and meeting new people fast.",
    },
    {
        "name": "Simona Yaneva",
        "phone": "+359 887 100 018",
        "description": "Dreamy and imaginative, into films, writing, and emotionally open conversations.",
    },
    {
        "name": "Martin Zhelev",
        "phone": "+359 887 100 019",
        "description": "Friendly and analytical, likes strategy games, startup ideas, and meeting thoughtful people.",
    },
    {
        "name": "Lora Peneva",
        "phone": "+359 887 100 020",
        "description": "Bright, sociable, and kind; enjoys museums, language learning, and introducing friends to each other.",
    },
]


class Command(BaseCommand):
    help = "Seeds the account system with 20 hardcoded example users for matching demos."

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        updated = 0

        for entry in DEMO_USERS:
            phone_number = entry["phone"]
            normalized_phone = normalize_phone_number(phone_number)
            profile = (
                AccountProfile.objects.select_related("user")
                .filter(normalized_phone_number=normalized_phone)
                .first()
            )

            if profile is None:
                username = build_voice_username(entry["name"], phone_number)
                user = User.objects.create_user(
                    username=username,
                    email="",
                    password=secrets.token_urlsafe(18),
                )
                profile = AccountProfile.objects.create(
                    user=user,
                    display_name=entry["name"],
                    phone_number=phone_number,
                    description=entry["description"],
                    dynamic_profile_summary=entry["description"],
                    onboarding_completed=True,
                    voice_navigation_enabled=True,
                    microphone_permission_granted=True,
                    phone_permission_granted=True,
                    contacts_permission_granted=True,
                    share_phone_with_friends=True,
                    share_email_with_friends=False,
                )
                created += 1
            else:
                profile.display_name = entry["name"]
                profile.phone_number = phone_number
                profile.description = entry["description"]
                profile.dynamic_profile_summary = entry["description"]
                profile.onboarding_completed = True
                profile.voice_navigation_enabled = True
                profile.microphone_permission_granted = True
                profile.phone_permission_granted = True
                profile.contacts_permission_granted = True
                profile.share_phone_with_friends = True
                profile.share_email_with_friends = False
                profile.save(
                    update_fields=[
                        "display_name",
                        "phone_number",
                        "description",
                        "dynamic_profile_summary",
                        "onboarding_completed",
                        "voice_navigation_enabled",
                        "microphone_permission_granted",
                        "phone_permission_granted",
                        "contacts_permission_granted",
                        "share_phone_with_friends",
                        "share_email_with_friends",
                    ]
                )
                updated += 1

            sync_profile_to_recommendations(profile, preserve_adaptation=False)

        for profile in AccountProfile.objects.select_related("user", "elder_profile").all():
            sync_profile_to_recommendations(profile, preserve_adaptation=True)
            seed_social_graph_for_profile(profile)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded demo users successfully. Created: {created}. Updated: {updated}."
            )
        )
