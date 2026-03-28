import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from recommendations.models import ElderProfile, SocialEdge

from .models import AccountProfile, FriendRequest, OnboardingDraft, RecommendationActivity
from .services import issue_token, recommend_profiles_for_viewer


class AccountApiTests(TestCase):
    def _auth_headers(self, user: User) -> dict:
        token = issue_token(user)
        return {"HTTP_AUTHORIZATION": f"Token {token.key}"}

    def _create_profile(
        self,
        *,
        username: str,
        email: str,
        phone_number: str = "",
        display_name: str | None = None,
        description: str = "",
        contacts_permission_granted: bool = False,
    ) -> AccountProfile:
        user = User.objects.create_user(
            username=username,
            email=email,
            password="StrongPass123!",
        )
        elder_profile = ElderProfile.objects.create(
            username=f"acct_{username}",
            display_name=display_name or username.title(),
            description=description,
        )
        return AccountProfile.objects.create(
            user=user,
            elder_profile=elder_profile,
            display_name=display_name or username.title(),
            phone_number=phone_number,
            description=description,
            contacts_permission_granted=contacts_permission_granted,
        )

    def _set_feature_vector(self, profile: AccountProfile, **overrides: float) -> None:
        vector = dict(profile.elder_profile.feature_vector or {})
        vector.update({key: float(value) for key, value in overrides.items()})
        profile.elder_profile.feature_vector = vector
        profile.elder_profile.feature_confidence = {
            feature_name: 1.0 for feature_name in profile.elder_profile.feature_vector
        }
        profile.elder_profile.save(update_fields=["feature_vector", "feature_confidence", "updated_at"])

    def test_issue_token_returns_jwt_and_me_accepts_it(self):
        profile = self._create_profile(
            username="jwt-user",
            email="jwt@example.com",
            phone_number="+359888111999",
            display_name="JWT User",
        )
        token = issue_token(profile.user)

        self.assertEqual(len(token.key.split(".")), 3)

        response = self.client.get(
            "/api/accounts/me/",
            **{"HTTP_AUTHORIZATION": f"Token {token.key}"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["profile"]["user_id"], profile.user_id)
        self.assertEqual(payload["profile"]["phone_number"], "+359888111999")

    @patch("apps.accounts.views.seed_social_graph_for_profile")
    @patch("apps.accounts.views.sync_profile_to_recommendations")
    def test_register_and_login_support_phone_first_voice_profile_fields(
        self,
        mock_sync,
        mock_seed,
    ):
        response = self.client.post(
            "/api/accounts/register/",
            data=json.dumps(
                {
                    "name": "Alice Stone",
                    "phone_number": "+359 888 111 222",
                    "description": "Warm, curious, and enjoys quiet coffee chats.",
                    "phone_permission_granted": True,
                    "microphone_permission_granted": True,
                    "contacts_permission_granted": True,
                    "onboarding_answers": {
                        "preferred_company": "One-to-one company feels best.",
                        "conversation_style": "Thoughtful and a good listener.",
                    },
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertIn("token", payload)
        self.assertEqual(payload["profile"]["display_name"], "Alice Stone")
        self.assertTrue(payload["profile"]["contacts_permission_granted"])
        self.assertTrue(payload["profile"]["phone_permission_granted"])
        self.assertTrue(payload["profile"]["onboarding_completed"])
        self.assertEqual(
            payload["profile"]["onboarding_answers"]["preferred_company"],
            "One-to-one company feels best.",
        )
        mock_sync.assert_called_once()
        mock_seed.assert_called_once()

        login_response = self.client.post(
            "/api/accounts/login/",
            data=json.dumps(
                {
                    "phone_number": "+359888111222",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["profile"]["display_name"], "Alice Stone")

    def test_register_returns_structured_field_errors(self):
        response = self.client.post(
            "/api/accounts/register/",
            data=json.dumps(
                {
                    "name": "Alice",
                    "phone_number": "",
                    "phone_permission_granted": False,
                    "microphone_permission_granted": False,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], "Sign up failed.")
        self.assertIn("errors", payload)
        self.assertIn("phone_number", payload["errors"])

    def test_onboarding_start_creates_session_id(self):
        response = self.client.post(
            "/api/accounts/onboarding/start/",
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "collecting")
        self.assertTrue(payload["draft"]["session_id"])
        self.assertTrue(
            OnboardingDraft.objects.filter(
                session_id=payload["draft"]["session_id"],
            ).exists()
        )

    def test_onboarding_turn_extracts_name_and_profile_from_free_form_text(self):
        start = self.client.post(
            "/api/accounts/onboarding/start/",
            data=json.dumps({}),
            content_type="application/json",
        ).json()
        session_id = start["draft"]["session_id"]

        response = self.client.post(
            "/api/accounts/onboarding/turn/",
            data=json.dumps(
                {
                    "session_id": session_id,
                    "message": "Аз съм Иван и обичам шах и разходки в парка.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["draft"]["display_name"], "Иван")
        self.assertIn("шах", payload["draft"]["dynamic_profile_summary"].lower())
        self.assertEqual(payload["mode"], "collecting")
        self.assertIn("phone_number", payload["missing_fields"])

    def test_onboarding_turn_with_existing_phone_switches_to_login_confirmation(self):
        existing = self._create_profile(
            username="ivan-existing",
            email="ivan@example.com",
            phone_number="+359888123456",
            display_name="Иван",
            description="Спокоен и общителен.",
        )
        start = self.client.post(
            "/api/accounts/onboarding/start/",
            data=json.dumps({}),
            content_type="application/json",
        ).json()

        response = self.client.post(
            "/api/accounts/onboarding/turn/",
            data=json.dumps(
                {
                    "session_id": start["draft"]["session_id"],
                    "message": "Аз съм Иван и телефонът ми е +359 888 123 456.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "login_confirmation")
        self.assertEqual(payload["recognized_phone"], existing.phone_number)

    def test_onboarding_confirm_login_returns_token_and_profile(self):
        self._create_profile(
            username="petya-existing",
            email="petya@example.com",
            phone_number="+359889123456",
            display_name="Петя",
            description="Обича спокойни разговори.",
        )
        start = self.client.post(
            "/api/accounts/onboarding/start/",
            data=json.dumps({}),
            content_type="application/json",
        ).json()
        session_id = start["draft"]["session_id"]

        self.client.post(
            "/api/accounts/onboarding/turn/",
            data=json.dumps(
                {
                    "session_id": session_id,
                    "message": "Телефонът ми е +359 889 123 456.",
                }
            ),
            content_type="application/json",
        )

        response = self.client.post(
            "/api/accounts/onboarding/confirm-login/",
            data=json.dumps(
                {
                    "session_id": session_id,
                    "phone_confirmed": True,
                    "login_confirmed": True,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("token", payload)
        self.assertEqual(payload["profile"]["display_name"], "Петя")
        self.assertEqual(payload["mode"], "completed")

    @patch("apps.accounts.onboarding_service.seed_social_graph_for_profile")
    @patch("apps.accounts.onboarding_service.sync_profile_to_recommendations")
    def test_onboarding_complete_registers_from_draft(
        self,
        mock_sync,
        mock_seed,
    ):
        start = self.client.post(
            "/api/accounts/onboarding/start/",
            data=json.dumps({}),
            content_type="application/json",
        ).json()
        session_id = start["draft"]["session_id"]

        self.client.post(
            "/api/accounts/onboarding/turn/",
            data=json.dumps(
                {
                    "session_id": session_id,
                    "message": "Аз съм Мария и обичам тихи разговори, цветя и дълги разходки.",
                }
            ),
            content_type="application/json",
        )
        self.client.post(
            "/api/accounts/onboarding/turn/",
            data=json.dumps(
                {
                    "session_id": session_id,
                    "message": "Телефонът ми е +359 887 654 321.",
                }
            ),
            content_type="application/json",
        )

        response = self.client.post(
            "/api/accounts/onboarding/complete/",
            data=json.dumps({"session_id": session_id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertIn("token", payload)
        self.assertEqual(payload["profile"]["display_name"], "Мария")
        self.assertIn("разходки", payload["profile"]["dynamic_profile_summary"])
        self.assertEqual(payload["mode"], "completed")
        mock_sync.assert_called_once()
        mock_seed.assert_called_once()

    @patch("apps.accounts.views.refresh_social_edge_for_friendship")
    def test_accepting_friend_request_unlocks_contact_details(self, mock_refresh_edge):
        alice = self._create_profile(
            username="alice",
            email="alice@example.com",
            display_name="Alice",
            description="Friendly and outgoing.",
        )
        bob = self._create_profile(
            username="bob",
            email="bob@example.com",
            phone_number="+359888555444",
            display_name="Bob",
            description="Calm and reflective.",
        )

        before_response = self.client.get(
            f"/api/accounts/users/{bob.user_id}/",
            **self._auth_headers(alice.user),
        )
        self.assertEqual(before_response.status_code, 200)
        before_payload = before_response.json()["profile"]
        self.assertEqual(before_payload["friend_status"], "none")
        self.assertIsNone(before_payload["email"])
        self.assertIsNone(before_payload["phone_number"])

        send_response = self.client.post(
            "/api/accounts/friend-requests/",
            data=json.dumps({"target_user_id": bob.user_id, "message": "Let us connect"}),
            content_type="application/json",
            **self._auth_headers(alice.user),
        )
        self.assertEqual(send_response.status_code, 201)
        request_id = send_response.json()["friend_request"]["id"]

        accept_response = self.client.post(
            f"/api/accounts/friend-requests/{request_id}/respond/",
            data=json.dumps({"action": "accept"}),
            content_type="application/json",
            **self._auth_headers(bob.user),
        )
        self.assertEqual(accept_response.status_code, 200)
        mock_refresh_edge.assert_called_once()

        after_response = self.client.get(
            f"/api/accounts/users/{bob.user_id}/",
            **self._auth_headers(alice.user),
        )
        after_payload = after_response.json()["profile"]
        self.assertEqual(after_payload["friend_status"], "accepted")
        self.assertEqual(after_payload["email"], "bob@example.com")
        self.assertEqual(after_payload["phone_number"], "+359888555444")

    def test_contact_import_matches_registered_users(self):
        owner = self._create_profile(
            username="owner",
            email="owner@example.com",
            display_name="Owner",
            contacts_permission_granted=True,
        )
        target = self._create_profile(
            username="target",
            email="target@example.com",
            phone_number="+359 888 999 000",
            display_name="Target Person",
            description="Enjoys history and tea.",
        )

        import_response = self.client.post(
            "/api/accounts/contacts/import/",
            data=json.dumps(
                {
                    "source": "device",
                    "contacts": [
                        {
                            "name": "Target Person",
                            "phone_number": "+359888999000",
                            "email": "target@example.com",
                        }
                    ],
                }
            ),
            content_type="application/json",
            **self._auth_headers(owner.user),
        )

        self.assertEqual(import_response.status_code, 200)
        payload = import_response.json()
        self.assertEqual(payload["imported_count"], 1)
        self.assertEqual(payload["matched_user_count"], 1)
        self.assertEqual(payload["contacts"][0]["matched_users"][0]["user_id"], target.user_id)
        self.assertIsNone(payload["contacts"][0]["matched_users"][0]["email"])

        search_response = self.client.get(
            "/api/accounts/search/?q=Target",
            **self._auth_headers(owner.user),
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertTrue(search_response.json()["results"][0]["matched_from_contacts"])

    @patch("recommendations.gat.recommender.get_embedding_snapshot")
    def test_discovery_returns_account_ready_recommendation_rows(self, mock_snapshot):
        viewer = self._create_profile(
            username="viewer",
            email="viewer@example.com",
            display_name="Viewer",
            description="Enjoys chess, thoughtful chats, and walking.",
        )
        match = self._create_profile(
            username="matchuser",
            email="match@example.com",
            display_name="Best Match",
            description="Enjoys chess, tea, and reflective conversations.",
        )
        other = self._create_profile(
            username="otheruser",
            email="other@example.com",
            display_name="Other Person",
            description="Prefers gardening and live music.",
        )

        class _Scalar:
            def __init__(self, value: float):
                self._value = value

            def item(self):
                return self._value

        class _Vec:
            def __init__(self, values):
                self.values = list(values)

            def __mul__(self, other):
                return _Vec([left * right for left, right in zip(self.values, other.values)])

            def sum(self):
                return _Scalar(sum(self.values))

        mock_snapshot.return_value = {
            "elder_ids": [viewer.elder_profile_id, match.elder_profile_id, other.elder_profile_id],
            "embeddings": [
                _Vec([1.0, 0.0]),
                _Vec([0.95, 0.05]),
                _Vec([0.1, 0.9]),
            ],
        }

        response = self.client.get(
            "/api/accounts/discovery/",
            **self._auth_headers(viewer.user),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["results"][0]["user_id"], match.user_id)
        self.assertEqual(payload["results"][0]["display_name"], "Best Match")
        self.assertEqual(payload["results"][0]["friend_status"], "none")
        self.assertIn("match_summary", payload["results"][0])
        self.assertIn("contact_access", payload["results"][0])
        self.assertIn("graph_score", payload["results"][0])
        self.assertIn("match_percent", payload["results"][0])
        self.assertIn("raw_score", payload["results"][0])
        self.assertIn("score_components", payload["results"][0])
        self.assertIn("semantic_similarity", payload["results"][0]["score_components"])
        self.assertIn("friendship_signal", payload["results"][0]["score_components"])
        self.assertIn("final_score", payload["results"][0]["score_components"])

        filtered = self.client.get(
            "/api/accounts/discovery/?q=Best",
            **self._auth_headers(viewer.user),
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["count"], 1)
        self.assertEqual(filtered.json()["results"][0]["user_id"], match.user_id)

    @patch("recommendations.gat.recommender.get_embedding_snapshot")
    def test_graph_scores_are_calibrated_when_embedding_similarity_is_overconfident(
        self,
        mock_snapshot,
    ):
        viewer = self._create_profile(
            username="graphviewer",
            email="graphviewer@example.com",
            display_name="Graph Viewer",
            description="Loves parks and active outdoor walks.",
        )
        target = self._create_profile(
            username="graphtarget",
            email="graphtarget@example.com",
            display_name="Graph Target",
            description="Avoids parks and prefers quiet indoor time.",
        )

        viewer.elder_profile.feature_vector.update(
            {
                "interest_nature": 1.0,
                "interest_sports": 0.9,
                "activity_level": 0.9,
                "prefers_small_groups": 0.2,
                "adventure_comfort": 0.9,
            }
        )
        viewer.elder_profile.feature_confidence = {name: 1.0 for name in viewer.elder_profile.feature_vector}
        viewer.elder_profile.save(update_fields=["feature_vector", "feature_confidence", "updated_at"])

        target.elder_profile.feature_vector.update(
            {
                "interest_nature": 0.0,
                "interest_sports": 0.1,
                "activity_level": 0.1,
                "prefers_small_groups": 0.9,
                "adventure_comfort": 0.1,
            }
        )
        target.elder_profile.feature_confidence = {name: 1.0 for name in target.elder_profile.feature_vector}
        target.elder_profile.save(update_fields=["feature_vector", "feature_confidence", "updated_at"])

        class _Scalar:
            def __init__(self, value: float):
                self._value = value

            def item(self):
                return self._value

        class _Vec:
            def __init__(self, values):
                self.values = list(values)

            def __mul__(self, other):
                return _Vec([left * right for left, right in zip(self.values, other.values)])

            def sum(self):
                return _Scalar(sum(self.values))

        mock_snapshot.return_value = {
            "elder_ids": [viewer.elder_profile_id, target.elder_profile_id],
            "embeddings": [
                _Vec([1.0, 0.0]),
                _Vec([0.999, 0.001]),
            ],
        }

        response = self.client.get(
            f"/api/accounts/users/{target.user_id}/",
            **self._auth_headers(viewer.user),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["profile"]
        self.assertLess(payload["graph_score"], 0.75)
        self.assertLess(payload["match_summary"]["compatibility_score"], 0.50)

    def test_refresh_social_edge_for_friendship_uses_compatibility_aware_weight(self):
        left = self._create_profile(
            username="edgeleft",
            email="edgeleft@example.com",
            display_name="Edge Left",
            description="Strong park lover.",
        )
        right = self._create_profile(
            username="edgeright",
            email="edgeright@example.com",
            display_name="Edge Right",
            description="Strong park avoider.",
        )

        left.elder_profile.feature_vector.update(
            {
                "interest_nature": 1.0,
                "interest_sports": 0.9,
                "activity_level": 0.9,
            }
        )
        left.elder_profile.feature_confidence = {name: 1.0 for name in left.elder_profile.feature_vector}
        left.elder_profile.save(update_fields=["feature_vector", "feature_confidence", "updated_at"])

        right.elder_profile.feature_vector.update(
            {
                "interest_nature": 0.0,
                "interest_sports": 0.1,
                "activity_level": 0.1,
            }
        )
        right.elder_profile.feature_confidence = {name: 1.0 for name in right.elder_profile.feature_vector}
        right.elder_profile.save(update_fields=["feature_vector", "feature_confidence", "updated_at"])

        from .services import refresh_social_edge_for_friendship

        edge = refresh_social_edge_for_friendship(left, right)

        self.assertIsNotNone(edge)
        self.assertLess(edge.gat_weight, 0.75)

    def test_accepted_friendship_adds_only_a_modest_bonus(self):
        viewer = self._create_profile(
            username="quietviewer",
            email="quietviewer@example.com",
            display_name="Quiet Viewer",
            description="Likes deep philosophical conversations, matcha, and calm cafes.",
        )
        target = self._create_profile(
            username="loudfriend",
            email="loudfriend@example.com",
            display_name="Loud Friend",
            description="Likes loud parties and outdoor sports.",
        )

        self._set_feature_vector(
            viewer,
            extroversion=0.15,
            prefers_small_groups=0.92,
            conversation_depth=0.95,
            activity_level=0.20,
            adventure_comfort=0.15,
            interest_arts=0.85,
            interest_sports=0.10,
            interest_nature=0.20,
        )
        self._set_feature_vector(
            target,
            extroversion=0.92,
            prefers_small_groups=0.12,
            conversation_depth=0.20,
            activity_level=0.95,
            adventure_comfort=0.90,
            interest_arts=0.15,
            interest_sports=0.95,
            interest_nature=0.90,
        )

        before_row = next(
            item for item in recommend_profiles_for_viewer(viewer, limit=5) if item["user_id"] == target.user_id
        )

        FriendRequest.objects.create(
            from_profile=viewer,
            to_profile=target,
            status=FriendRequest.Status.ACCEPTED,
            responded_at=timezone.now(),
        )
        SocialEdge.upsert(viewer.elder_profile, target.elder_profile, 0.32)

        after_row = next(
            item for item in recommend_profiles_for_viewer(viewer, limit=5) if item["user_id"] == target.user_id
        )

        self.assertAlmostEqual(
            before_row["score_components"]["semantic_similarity"],
            after_row["score_components"]["semantic_similarity"],
            places=4,
        )
        self.assertGreater(after_row["raw_score"], before_row["raw_score"])
        self.assertLess(after_row["raw_score"] - before_row["raw_score"], 0.08)
        self.assertGreater(after_row["score_components"]["friendship_signal"], 0.0)
        self.assertGreater(after_row["score_components"]["friendship_bonus"], 0.0)
        self.assertLess(after_row["score_components"]["semantic_similarity"], 0.65)

    def test_semantic_match_can_still_outrank_accepted_friend(self):
        viewer = self._create_profile(
            username="thoughtfulviewer",
            email="thoughtfulviewer@example.com",
            display_name="Thoughtful Viewer",
            description="Likes deep philosophical conversations, matcha, and calm cafes.",
        )
        accepted_friend = self._create_profile(
            username="partyfriend",
            email="partyfriend@example.com",
            display_name="Party Friend",
            description="Likes loud parties and outdoor sports.",
        )
        semantic_match = self._create_profile(
            username="calmmatch",
            email="calmmatch@example.com",
            display_name="Calm Match",
            description="Enjoys reflective talks, tea rituals, and quiet cafe afternoons.",
        )

        self._set_feature_vector(
            viewer,
            extroversion=0.20,
            prefers_small_groups=0.90,
            conversation_depth=0.96,
            activity_level=0.25,
            adventure_comfort=0.20,
            interest_arts=0.88,
            interest_sports=0.12,
            interest_nature=0.25,
            listening_style=0.90,
        )
        self._set_feature_vector(
            accepted_friend,
            extroversion=0.95,
            prefers_small_groups=0.10,
            conversation_depth=0.22,
            activity_level=0.94,
            adventure_comfort=0.95,
            interest_arts=0.15,
            interest_sports=0.94,
            interest_nature=0.92,
            listening_style=0.25,
        )
        self._set_feature_vector(
            semantic_match,
            extroversion=0.30,
            prefers_small_groups=0.85,
            conversation_depth=0.92,
            activity_level=0.32,
            adventure_comfort=0.30,
            interest_arts=0.83,
            interest_sports=0.18,
            interest_nature=0.30,
            listening_style=0.86,
        )

        FriendRequest.objects.create(
            from_profile=viewer,
            to_profile=accepted_friend,
            status=FriendRequest.Status.ACCEPTED,
            responded_at=timezone.now(),
        )
        SocialEdge.upsert(viewer.elder_profile, accepted_friend.elder_profile, 0.34)

        rows = recommend_profiles_for_viewer(viewer, limit=5)
        self.assertEqual(rows[0]["user_id"], semantic_match.user_id)

        rows_by_user_id = {item["user_id"]: item for item in rows}
        friend_row = rows_by_user_id[accepted_friend.user_id]
        semantic_row = rows_by_user_id[semantic_match.user_id]

        self.assertGreater(semantic_row["score_components"]["semantic_similarity"], 0.70)
        self.assertLess(friend_row["score_components"]["semantic_similarity"], 0.65)
        self.assertGreater(friend_row["score_components"]["friendship_signal"], 0.0)
        self.assertGreater(semantic_row["raw_score"], friend_row["raw_score"])

    def test_description_query_ranks_by_typed_persona(self):
        viewer = self._create_profile(
            username="viewer",
            email="viewer@example.com",
            display_name="Viewer",
            description="Calm, thoughtful, prefers quiet coffee and reading.",
        )
        sporty = self._create_profile(
            username="sporty",
            email="sporty@example.com",
            display_name="Sporty Friend",
            description="Loves volleyball, active weekends, energetic team games.",
        )
        quiet = self._create_profile(
            username="quiet",
            email="quiet@example.com",
            display_name="Quiet Friend",
            description="Enjoys books, reflective talks, and peaceful afternoons.",
        )

        response = self.client.post(
            "/api/accounts/discovery/query/",
            data=json.dumps(
                {
                    "description": "Someone active who enjoys volleyball and energetic group activities.",
                    "limit": 5,
                }
            ),
            content_type="application/json",
            **self._auth_headers(viewer.user),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["results"][0]["user_id"], sporty.user_id)
        self.assertEqual(payload["results"][0]["discovery_mode"], "describe_someone")
        self.assertGreaterEqual(
            RecommendationActivity.objects.filter(
                actor_profile=viewer,
                event_type=RecommendationActivity.EventType.DESCRIPTION_QUERY_SUBMITTED,
            ).count(),
            1,
        )
        self.assertNotEqual(payload["results"][0]["user_id"], quiet.user_id)

    def test_activity_endpoint_logs_social_signal(self):
        viewer = self._create_profile(
            username="viewer",
            email="viewer@example.com",
            display_name="Viewer",
            description="Warm and curious.",
        )
        target = self._create_profile(
            username="target",
            email="target@example.com",
            display_name="Target",
            description="Friendly and playful.",
        )

        response = self.client.post(
            "/api/accounts/activities/",
            data=json.dumps(
                {
                    "event_type": "profile_viewed",
                    "target_user_id": target.user_id,
                    "discovery_mode": "for_you",
                    "metadata": {"source": "result_card"},
                }
            ),
            content_type="application/json",
            **self._auth_headers(viewer.user),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            RecommendationActivity.objects.filter(
                actor_profile=viewer,
                target_profile=target,
                event_type=RecommendationActivity.EventType.PROFILE_VIEWED,
            ).count(),
            1,
        )

    @patch("apps.accounts.agent_service.sync_profile_to_recommendations")
    @patch("apps.accounts.agent_service.QwenWorkerClient.generate")
    def test_agent_profile_update_endpoint_updates_profile(self, mock_generate, mock_sync):
        viewer = self._create_profile(
            username="viewer",
            email="viewer@example.com",
            display_name="Viewer",
            description="Warm and curious.",
        )
        mock_generate.return_value = json.dumps(
            {
                "description": "Warm, curious, and loves thoughtful coffee chats.",
                "dynamic_profile_summary": "Thoughtful and warm.",
                "profile_notes": "Prefers calm one-to-one conversations.",
                "reasoning_summary": "Merged new stable preferences into the profile.",
            }
        )
        mock_sync.return_value = SimpleNamespace(
            id=viewer.elder_profile_id,
            feature_vector_version=7,
            vector_source="account_onboarding",
        )

        response = self.client.post(
            "/api/accounts/agent/profile/update/",
            data=json.dumps(
                {
                    "user_id": str(viewer.user_id),
                    "prompt": "I love thoughtful coffee chats and I open up more in calm one-to-one conversations.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["profile"]["description"],
            "Warm, curious, and loves thoughtful coffee chats.",
        )
        self.assertEqual(
            payload["profile"]["dynamic_profile_summary"],
            "Thoughtful and warm.",
        )
        self.assertEqual(
            payload["vector_profile"]["feature_vector_version"],
            7,
        )

        viewer.refresh_from_db()
        self.assertEqual(
            viewer.profile_notes,
            "Prefers calm one-to-one conversations.",
        )

    @patch("apps.accounts.agent_service.record_recommendation_activity")
    @patch("apps.accounts.agent_service.QwenWorkerClient.generate")
    def test_agent_find_connection_endpoint_returns_board_ready_user(self, mock_generate, mock_activity):
        viewer = self._create_profile(
            username="viewer",
            email="viewer@example.com",
            display_name="Viewer",
            description="Calm, thoughtful, prefers quiet coffee and reading.",
        )
        sporty = self._create_profile(
            username="sporty",
            email="sporty@example.com",
            display_name="Sporty Friend",
            description="Loves volleyball, active weekends, energetic team games.",
        )
        self._create_profile(
            username="quiet",
            email="quiet@example.com",
            display_name="Quiet Friend",
            description="Enjoys books, reflective talks, and peaceful afternoons.",
        )

        mock_generate.return_value = json.dumps(
            {
                "temporary_description": "Someone active who enjoys volleyball and energetic group activities.",
                "reasoning_summary": "Turned the prompt into a social search description.",
            }
        )

        response = self.client.post(
            "/api/accounts/agent/connections/find/",
            data=json.dumps(
                {
                    "user_id": str(viewer.user_id),
                    "prompt": "Find me someone active who likes volleyball and energetic group activities.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["user"]["user_id"], sporty.user_id)
        self.assertEqual(payload["board_object"]["extra_data"]["kind"], "user")
        self.assertIn("type:user", payload["board_object"]["tags"])
        self.assertEqual(
            payload["board_object"]["extra_data"]["description"],
            sporty.description,
        )
        mock_activity.assert_called_once()
