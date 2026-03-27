import json

from django.test import TestCase

from recommendations.services.feature_extraction import extract_preference_intents


class BulgarianSemanticNegationTests(TestCase):
    def test_extracts_opposite_polarity_for_same_object(self):
        positive = extract_preference_intents("обичам мъже")
        negative = extract_preference_intents("не обичам мъже")

        self.assertTrue(any(item.get("object") == "мъже" and item.get("polarity") == 1 for item in positive))
        self.assertTrue(any(item.get("object") == "мъже" and item.get("polarity") == -1 for item in negative))

    def test_compare_users_opposite_polarity_scores_low(self):
        response = self.client.post(
            "/api/recommendations/compare/",
            data=json.dumps(
                {
                    "left_description": "обичам мъже",
                    "right_description": "не обичам мъже",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertLess(payload["compatibility_score"], 0.40)

    def test_compare_users_semantic_similarity_stays_high_for_true_match(self):
        response = self.client.post(
            "/api/recommendations/compare/",
            data=json.dumps(
                {
                    "left_description": "обичам кафе",
                    "right_description": "харесвам кафе",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreater(payload["compatibility_score"], 0.70)

    def test_compare_users_negative_pair_scores_low(self):
        response = self.client.post(
            "/api/recommendations/compare/",
            data=json.dumps(
                {
                    "left_description": "обичам паркове",
                    "right_description": "не харесвам паркове",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertLess(payload["compatibility_score"], 0.40)
