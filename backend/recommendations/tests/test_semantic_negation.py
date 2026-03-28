import json

from django.test import TestCase

from recommendations.gat.feature_schema import get_default_feature_vector
from recommendations.services.compatibility_engine import compare_people
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

    def test_extracts_negated_bulgarian_tolerance_phrase(self):
        positive = extract_preference_intents("\u041e\u0431\u0438\u0447\u0430\u043c \u0442\u0438\u0445\u0438 \u043a\u0430\u0444\u0435\u043d\u0435\u0442\u0430")
        negative = extract_preference_intents(
            "\u041d\u0435 \u043f\u043e\u043d\u0430\u0441\u044f\u043c \u0442\u0438\u0445\u0438 \u043a\u0430\u0444\u0435\u043d\u0435\u0442\u0430"
        )

        self.assertTrue(
            any(item.get("object") == "\u0442\u0438\u0445\u0438" and item.get("polarity") == 1 for item in positive)
        )
        self.assertTrue(
            any(item.get("object") == "\u0442\u0438\u0445\u0438" and item.get("polarity") == -1 for item in negative)
        )

    def test_compare_people_penalizes_opposite_bulgarian_intents(self):
        quiet_text = (
            "\u041e\u0431\u0438\u0447\u0430\u043c \u0442\u0438\u0448\u0438\u043d\u0430, "
            "\u043a\u043d\u0438\u0433\u0438 \u0438 \u0441\u043f\u043e\u043a\u043e\u0439\u043d\u0438 "
            "\u043a\u0430\u0444\u0435\u043d\u0435\u0442\u0430. \u041d\u0435 \u043f\u043e\u043d\u0430\u0441\u044f\u043c "
            "\u0448\u0443\u043c\u043d\u0438 \u043f\u0430\u0440\u0442\u0438\u0442\u0430."
        )
        loud_text = (
            "\u041e\u0431\u0438\u0447\u0430\u043c \u0448\u0443\u043c\u043d\u0438 "
            "\u043f\u0430\u0440\u0442\u0438\u0442\u0430. \u041d\u0435 \u043f\u043e\u043d\u0430\u0441\u044f\u043c "
            "\u0442\u0438\u0448\u0438\u043d\u0430 \u0438 \u0441\u043f\u043e\u043a\u043e\u0439\u043d\u0438 "
            "\u043a\u0430\u0444\u0435\u043d\u0435\u0442\u0430."
        )

        comparison = compare_people(
            get_default_feature_vector(),
            get_default_feature_vector(),
            left_intents=extract_preference_intents(quiet_text),
            right_intents=extract_preference_intents(loud_text),
        )

        self.assertLess(comparison["compatibility_score"], 0.15)
        self.assertGreaterEqual(comparison["score_breakdown"]["intent_contradictions"], 1)
        self.assertIn("\u0442\u0438\u0448\u0438\u043d\u0430", comparison["score_breakdown"]["intent_overlap_items"])
