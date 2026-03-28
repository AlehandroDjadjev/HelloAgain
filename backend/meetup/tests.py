import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import AccountProfile, FriendRequest
from apps.accounts.services import issue_token
from .models import MeetupInvite, MeetupNotification

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
		self.assertIn('graph_place_score', rows[0]['score_breakdown'])
		self.assertGreaterEqual(rows[0]['score_breakdown']['graph_place_score'], 0.5)

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

	def test_pair_similarity_does_not_change_numeric_place_score(self, *_):
		coordinates = [{'lat': 42.69, 'lng': 23.34}, {'lat': 42.688, 'lng': 23.335}]
		descriptions = [
			'I love parks and quiet outdoor talks.',
			'I prefer cafes and calm conversations.',
		]

		with patch('meetup.services._pair_user_similarity', return_value=0.0):
			low_similarity_rows = get_ranked_meetup_spots(
				coordinates=coordinates,
				participant_descriptions=descriptions,
				top_n=3,
			)

		with patch('meetup.services._pair_user_similarity', return_value=1.0):
			high_similarity_rows = get_ranked_meetup_spots(
				coordinates=coordinates,
				participant_descriptions=descriptions,
				top_n=3,
			)

		self.assertTrue(low_similarity_rows)
		self.assertTrue(high_similarity_rows)
		self.assertEqual(low_similarity_rows[0]['place_name'], high_similarity_rows[0]['place_name'])
		self.assertEqual(low_similarity_rows[0]['score'], high_similarity_rows[0]['score'])
		self.assertEqual(low_similarity_rows[0]['score_breakdown']['user_similarity_weight'], 0.0)
		self.assertEqual(high_similarity_rows[0]['score_breakdown']['user_similarity_weight'], 0.0)
		self.assertEqual(low_similarity_rows[0]['user_similarity_score'], 0)
		self.assertEqual(high_similarity_rows[0]['user_similarity_score'], 100)

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


class MeetupInviteNotificationApiTests(TestCase):
	def setUp(self):
		super().setUp()
		self.requester_user = User.objects.create_user(username='alice', password='x')
		self.invited_user = User.objects.create_user(username='bob', password='x')

		self.requester_profile = AccountProfile.objects.create(
			user=self.requester_user,
			display_name='Alice',
			description='Обичам разходки и кафе.',
			home_lat=42.69,
			home_lng=23.34,
		)
		self.invited_profile = AccountProfile.objects.create(
			user=self.invited_user,
			display_name='Bob',
			description='Предпочитам спокойни разговори.',
			home_lat=42.688,
			home_lng=23.335,
		)

		FriendRequest.objects.create(
			from_profile=self.requester_profile,
			to_profile=self.invited_profile,
			status=FriendRequest.Status.ACCEPTED,
		)

		self.requester_token = issue_token(self.requester_user)
		self.invited_token = issue_token(self.invited_user)

	def _headers(self, token_key: str) -> dict:
		return {'HTTP_AUTHORIZATION': f'Token {token_key}'}

	@patch('meetup.views.get_best_meetup_spot')
	def test_propose_returns_day_date_time_and_creates_invite_notification(self, mock_best):
		mock_best.return_value = {
			'place_name': 'Talk Cafe',
			'place_lat': 42.688,
			'place_lng': 23.335,
			'weather': 'Ясно',
			'temperature': 23,
			'score': 82,
			'recommended_time': '2026-03-28 16:00',
		}
		proposed = (timezone.now() + timedelta(hours=2)).replace(second=0, microsecond=0)

		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps(
				{
					'friend_user_id': self.invited_user.id,
					'proposed_time': proposed.isoformat(),
				}
			),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 201)
		body = response.json()
		invite = body['invite']
		self.assertIn('meeting_day_bg', invite)
		self.assertIn('meeting_date_bg', invite)
		self.assertIn('meeting_time_bg', invite)
		self.assertIn('meeting_when_bg', invite)

		notification = body['notification']
		self.assertEqual(notification['type'], MeetupNotification.Type.INVITE_REQUEST)
		self.assertEqual(MeetupNotification.objects.count(), 1)
		note_obj = MeetupNotification.objects.first()
		self.assertEqual(note_obj.recipient_profile_id, self.invited_profile.id)

	@patch('meetup.views.get_best_meetup_spot')
	def test_propose_accepts_friend_name_and_resolves_only_accepted_friend(self, mock_best):
		mock_best.return_value = {
			'place_name': 'Talk Cafe',
			'place_lat': 42.688,
			'place_lng': 23.335,
			'weather': 'Ясно',
			'temperature': 23,
			'score': 82,
			'recommended_time': '2026-03-28 16:00',
		}

		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps({'friend_name': self.invited_profile.display_name}),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 201)
		self.assertEqual(response.json()['invite']['invited_user_id'], self.invited_user.id)

	def test_propose_rejects_non_friend_user_id(self):
		outsider_user = User.objects.create_user(username='outsider', password='x')
		AccountProfile.objects.create(
			user=outsider_user,
			display_name='Outsider',
			home_lat=42.681,
			home_lng=23.321,
		)

		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps({'friend_user_id': outsider_user.id}),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 403)
		self.assertEqual(response.json()['code'], 'FRIEND_REQUIRED')

	def test_propose_rejects_invalid_friend_user_id(self):
		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps({'friend_user_id': 'not-a-number'}),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['code'], 'INVALID_FRIEND')

	def test_propose_rejects_unknown_friend_name(self):
		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps({'friend_name': 'Несъществуващ приятел'}),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 404)
		self.assertEqual(response.json()['code'], 'FRIEND_NOT_FOUND')

	def test_propose_rejects_ambiguous_friend_name(self):
		duplicate_user = User.objects.create_user(username='bob-2', password='x')
		duplicate_profile = AccountProfile.objects.create(
			user=duplicate_user,
			display_name=self.invited_profile.display_name,
			home_lat=42.682,
			home_lng=23.322,
		)
		FriendRequest.objects.create(
			from_profile=self.requester_profile,
			to_profile=duplicate_profile,
			status=FriendRequest.Status.ACCEPTED,
		)

		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps({'friend_name': self.invited_profile.display_name}),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['code'], 'FRIEND_NAME_AMBIGUOUS')

	def test_propose_rejects_multiple_friend_selectors(self):
		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps(
				{
					'friend_name': self.invited_profile.display_name,
					'friend_user_id': self.invited_user.id,
				}
			),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()['code'], 'MULTIPLE_FRIEND_SELECTORS')

	def test_accept_creates_requester_notification_and_two_reminders(self):
		proposed = timezone.now() + timedelta(hours=1)
		invite = MeetupInvite.objects.create(
			requester_profile=self.requester_profile,
			invited_profile=self.invited_profile,
			proposed_time=proposed,
			place_name='Talk Cafe',
			place_lat=42.688,
			place_lng=23.335,
			center_lat=42.689,
			center_lng=23.336,
		)
		request_note = MeetupNotification.objects.create(
			recipient_profile=self.invited_profile,
			invite=invite,
			notification_type=MeetupNotification.Type.INVITE_REQUEST,
			title='Нова покана за среща',
			body='Alice предлага среща.',
			payload={'requires_response': True},
		)

		response = self.client.post(
			f'/api/meetup/friends/invites/{invite.id}/respond/',
			data=json.dumps({'action': 'accept'}),
			content_type='application/json',
			**self._headers(self.invited_token.key),
		)

		self.assertEqual(response.status_code, 200)
		invite.refresh_from_db()
		request_note.refresh_from_db()
		self.assertEqual(invite.status, MeetupInvite.Status.ACCEPTED)
		self.assertIsNotNone(request_note.read_at)

		notes = MeetupNotification.objects.filter(invite=invite)
		self.assertEqual(notes.count(), 4)
		self.assertEqual(
			notes.filter(notification_type=MeetupNotification.Type.INVITE_ACCEPTED).count(),
			1,
		)
		reminders = notes.filter(notification_type=MeetupNotification.Type.REMINDER_20M)
		self.assertEqual(reminders.count(), 2)
		expected_time = invite.proposed_time - timedelta(minutes=20)
		for reminder in reminders:
			self.assertEqual(reminder.scheduled_for, expected_time)

	def test_notifications_feed_hides_future_reminders_until_due(self):
		invite = MeetupInvite.objects.create(
			requester_profile=self.requester_profile,
			invited_profile=self.invited_profile,
			status=MeetupInvite.Status.ACCEPTED,
			proposed_time=timezone.now() + timedelta(hours=1),
			place_name='Talk Cafe',
			place_lat=42.688,
			place_lng=23.335,
			center_lat=42.689,
			center_lng=23.336,
		)
		future_reminder = MeetupNotification.objects.create(
			recipient_profile=self.requester_profile,
			invite=invite,
			notification_type=MeetupNotification.Type.REMINDER_20M,
			title='Напомняне',
			body='Срещата започва скоро.',
			scheduled_for=timezone.now() + timedelta(minutes=25),
		)
		ready_reminder = MeetupNotification.objects.create(
			recipient_profile=self.requester_profile,
			invite=invite,
			notification_type=MeetupNotification.Type.REMINDER_20M,
			title='Напомняне',
			body='Срещата започва след 20 минути.',
			scheduled_for=timezone.now() - timedelta(minutes=1),
		)

		response = self.client.get(
			'/api/meetup/friends/notifications/',
			**self._headers(self.requester_token.key),
		)

		self.assertEqual(response.status_code, 200)
		body = response.json()
		notification_ids = {item['id'] for item in body['notifications']}
		due_ids = {item['id'] for item in body['due_reminders']}
		self.assertNotIn(future_reminder.id, notification_ids)
		self.assertIn(ready_reminder.id, notification_ids)
		self.assertEqual(due_ids, {ready_reminder.id})

	def test_decline_notifies_requester_that_no_one_accepted(self):
		invite = MeetupInvite.objects.create(
			requester_profile=self.requester_profile,
			invited_profile=self.invited_profile,
			proposed_time=timezone.now() + timedelta(hours=1),
			place_name='Talk Cafe',
			place_lat=42.688,
			place_lng=23.335,
			center_lat=42.689,
			center_lng=23.336,
		)
		request_note = MeetupNotification.objects.create(
			recipient_profile=self.invited_profile,
			invite=invite,
			notification_type=MeetupNotification.Type.INVITE_REQUEST,
			title='Нова покана за среща',
			body='Alice предлага среща.',
			payload={'requires_response': True},
		)

		response = self.client.post(
			f'/api/meetup/friends/invites/{invite.id}/respond/',
			data=json.dumps({'action': 'decline'}),
			content_type='application/json',
			**self._headers(self.invited_token.key),
		)

		self.assertEqual(response.status_code, 200)
		body = response.json()
		request_note.refresh_from_db()
		self.assertEqual(body['notifications'][0]['type'], MeetupNotification.Type.INVITE_DECLINED)
		self.assertTrue(body['notifications'][0]['payload']['all_declined'])
		self.assertIsNotNone(request_note.read_at)

		requester_notes = MeetupNotification.objects.filter(
			recipient_profile=self.requester_profile,
			invite=invite,
			notification_type=MeetupNotification.Type.INVITE_DECLINED,
		)
		self.assertEqual(requester_notes.count(), 1)

	def test_meeting_endpoint_returns_next_accepted_meeting(self):
		invite = MeetupInvite.objects.create(
			requester_profile=self.requester_profile,
			invited_profile=self.invited_profile,
			status=MeetupInvite.Status.ACCEPTED,
			proposed_time=timezone.now() + timedelta(hours=3),
			place_name='Talk Cafe',
			place_lat=42.688,
			place_lng=23.335,
			center_lat=42.689,
			center_lng=23.336,
		)

		response = self.client.get(
			'/api/meetup/friends/meeting/',
			**self._headers(self.requester_token.key),
		)
		self.assertEqual(response.status_code, 200)
		body = response.json()
		self.assertTrue(body['has_meeting'])
		self.assertEqual(body['meeting']['id'], invite.id)
		self.assertIn('meeting_day_bg', body['meeting'])

	@patch('meetup.views.get_best_meetup_spot')
	def test_cannot_propose_when_requester_already_has_accepted_meeting(self, mock_best):
		mock_best.return_value = {
			'place_name': 'Talk Cafe',
			'place_lat': 42.688,
			'place_lng': 23.335,
			'weather': 'Ясно',
			'temperature': 23,
			'score': 82,
			'recommended_time': '2026-03-28 16:00',
		}

		third_user = User.objects.create_user(username='charlie', password='x')
		third_profile = AccountProfile.objects.create(
			user=third_user,
			display_name='Charlie',
			home_lat=42.685,
			home_lng=23.330,
		)
		MeetupInvite.objects.create(
			requester_profile=third_profile,
			invited_profile=self.requester_profile,
			status=MeetupInvite.Status.ACCEPTED,
			proposed_time=timezone.now() + timedelta(hours=5),
			place_name='Existing Meetup',
			place_lat=42.686,
			place_lng=23.331,
			center_lat=42.686,
			center_lng=23.331,
		)

		response = self.client.post(
			'/api/meetup/friends/propose/',
			data=json.dumps({'friend_user_id': self.invited_user.id}),
			content_type='application/json',
			**self._headers(self.requester_token.key),
		)
		self.assertEqual(response.status_code, 409)
		self.assertEqual(response.json()['code'], 'MEETING_ALREADY_SCHEDULED')

	def test_cannot_accept_when_user_already_has_another_accepted_meeting(self):
		third_user = User.objects.create_user(username='david', password='x')
		third_profile = AccountProfile.objects.create(
			user=third_user,
			display_name='David',
			home_lat=42.684,
			home_lng=23.329,
		)

		MeetupInvite.objects.create(
			requester_profile=third_profile,
			invited_profile=self.invited_profile,
			status=MeetupInvite.Status.ACCEPTED,
			proposed_time=timezone.now() + timedelta(hours=4),
			place_name='Busy Slot',
			place_lat=42.684,
			place_lng=23.329,
			center_lat=42.684,
			center_lng=23.329,
		)

		pending = MeetupInvite.objects.create(
			requester_profile=self.requester_profile,
			invited_profile=self.invited_profile,
			status=MeetupInvite.Status.PENDING,
			proposed_time=timezone.now() + timedelta(hours=6),
			place_name='New Pending Meetup',
			place_lat=42.688,
			place_lng=23.335,
			center_lat=42.689,
			center_lng=23.336,
		)

		response = self.client.post(
			f'/api/meetup/friends/invites/{pending.id}/respond/',
			data=json.dumps({'action': 'accept'}),
			content_type='application/json',
			**self._headers(self.invited_token.key),
		)
		self.assertEqual(response.status_code, 409)
		self.assertEqual(response.json()['code'], 'MEETING_ALREADY_SCHEDULED')
		pending.refresh_from_db()
		self.assertEqual(pending.status, MeetupInvite.Status.PENDING)
