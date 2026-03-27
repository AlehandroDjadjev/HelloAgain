from django.test import TestCase
from unittest.mock import patch

from .services import get_ranked_meetup_spots


def _mock_places(*_, **__):
	return [
		{
			'place_id': 'park-1',
			'name': 'Borisova Park',
			'types': ['park'],
			'vicinity': 'Sofia',
			'rating': 4.6,
			'user_ratings_total': 500,
			'geometry': {'location': {'lat': 42.69, 'lng': 23.34}},
		},
		{
			'place_id': 'library-1',
			'name': 'City Library',
			'types': ['library'],
			'vicinity': 'Sofia',
			'rating': 4.7,
			'user_ratings_total': 210,
			'geometry': {'location': {'lat': 42.687, 'lng': 23.332}},
		},
		{
			'place_id': 'cafe-1',
			'name': 'Talk Cafe',
			'types': ['cafe'],
			'vicinity': 'Sofia',
			'rating': 4.5,
			'user_ratings_total': 330,
			'geometry': {'location': {'lat': 42.688, 'lng': 23.335}},
		},
	]


def _mock_weather(*_, **__):
	return [
		{
			'dt': 1760000000,
			'main': {'temp': 22},
			'wind': {'speed': 4},
			'rain': {'3h': 0},
			'weather': [{'main': 'Clear'}],
		},
		{
			'dt': 1760010800,
			'main': {'temp': 21},
			'wind': {'speed': 3},
			'rain': {'3h': 0},
			'weather': [{'main': 'Clouds'}],
		},
	]


def _mock_weather_with_rainy_slot(*_, **__):
	return [
		{
			'dt': 1760000000,  # 11:00
			'main': {'temp': 22},
			'wind': {'speed': 4},
			'rain': {'3h': 0},
			'weather': [{'main': 'Clear'}],
		},
		{
			'dt': 1760021600,  # later afternoon/evening
			'main': {'temp': 17},
			'wind': {'speed': 7},
			'rain': {'3h': 2.4},
			'weather': [{'main': 'Rain'}],
		},
	]


@patch('meetup.services.fetch_places', side_effect=_mock_places)
@patch('meetup.services.fetch_weather', side_effect=_mock_weather)
class MeetupSemanticRankingTests(TestCase):
	def test_bulgarian_park_contradiction_not_100_similarity_and_not_top_park(self, *_):
		rows = get_ranked_meetup_spots(
			coordinates=[{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}],
			participant_descriptions=[
				'Обичам разходки в парка и природа.',
				'Не харесвам разходки в парка, предпочитам кафе и разговори вътре.',
			],
			top_n=3,
		)

		self.assertTrue(rows)
		self.assertLess(int(rows[0]['user_similarity_score']), 90)
		self.assertNotEqual(rows[0]['types'][0], 'park')

	def test_park_preference_prefers_park_place(self, *_):
		rows = get_ranked_meetup_spots(
			coordinates=[{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}],
			participant_descriptions=[
				'I enjoy long walks in the park and outdoor relaxing talks.',
				'I also love park walks and being outside in green spaces.',
			],
			top_n=3,
		)

		self.assertTrue(rows)
		self.assertEqual(rows[0]['types'][0], 'park')

	def test_similar_interests_different_tone_remain_high(self, *_):
		rows = get_ranked_meetup_spots(
			coordinates=[{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}],
			participant_descriptions=[
				'I enjoy books and history museums with calm conversations.',
				'Love reading, historical talks and reflective chats over coffee.',
			],
			top_n=3,
		)

		self.assertTrue(rows)
		self.assertGreaterEqual(int(rows[0]['user_similarity_score']), 70)

	def test_conflicting_preferences_drop_similarity(self, *_):
		rows = get_ranked_meetup_spots(
			coordinates=[{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}],
			participant_descriptions=[
				'I love volleyball, sports and outdoor park activities.',
				'I dislike sports and avoid parks, I prefer books and indoor talks.',
			],
			top_n=3,
		)

		self.assertTrue(rows)
		self.assertLess(int(rows[0]['user_similarity_score']), 40)

	def test_very_short_descriptions_do_not_crash(self, *_):
		rows = get_ranked_meetup_spots(
			coordinates=[{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}],
			participant_descriptions=['hi', 'yo'],
			top_n=3,
		)

		self.assertTrue(rows)
		self.assertIn('explanation', rows[0])

	@patch('meetup.services.fetch_weather', side_effect=_mock_weather_with_rainy_slot)
	def test_weather_preference_avoids_rainy_slot(self, *_):
		rows = get_ranked_meetup_spots(
			coordinates=[{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}],
			participant_descriptions=[
				'I love clear sunny weather and I avoid rain for meetups.',
				'Prefer sunny weather too, no rain and low wind please.',
			],
			top_n=3,
		)

		self.assertTrue(rows)
		self.assertEqual(rows[0]['weather'], 'Ясно')
		self.assertEqual(rows[0]['score_breakdown']['weather_weight'], 0.2)
