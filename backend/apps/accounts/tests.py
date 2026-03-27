import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from recommendations.models import ElderProfile

from .models import AccountProfile, RecommendationActivity
from .services import issue_token


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

    @patch("apps.accounts.views.sync_profile_to_recommendations")
    def test_register_and_login_support_profile_fields(self, mock_sync):
        response = self.client.post(
            "/api/accounts/register/",
            data=json.dumps(
                {
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": "StrongPass123!",
                    "display_name": "Alice",
                    "phone_number": "+359 888 111 222",
                    "description": "Warm, curious, and enjoys quiet coffee chats.",
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
        self.assertEqual(payload["profile"]["display_name"], "Alice")
        self.assertTrue(payload["profile"]["contacts_permission_granted"])
        self.assertEqual(
            payload["profile"]["onboarding_answers"]["preferred_company"],
            "One-to-one company feels best.",
        )
        mock_sync.assert_called_once()

        login_response = self.client.post(
            "/api/accounts/login/",
            data=json.dumps(
                {
                    "identifier": "alice@example.com",
                    "password": "StrongPass123!",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["profile"]["username"], "alice")

    def test_register_returns_structured_field_errors(self):
        response = self.client.post(
            "/api/accounts/register/",
            data=json.dumps(
                {
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": "123",
                    "display_name": "Alice",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], "Sign up failed.")
        self.assertIn("errors", payload)
        self.assertIn("password", payload["errors"])

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
        self.assertGreaterEqual(payload["results"][0]["match_percent"], 70)

        filtered = self.client.get(
            "/api/accounts/discovery/?q=Best",
            **self._auth_headers(viewer.user),
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["count"], 1)
        self.assertEqual(filtered.json()["results"][0]["user_id"], match.user_id)

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
