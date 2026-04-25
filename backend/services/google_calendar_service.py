from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

import requests
from django.utils import timezone

from apps.accounts.models import AccountProfile, GoogleCalendarConnection
from apps.accounts.google_calendar_crypto import encrypt_token
from apps.accounts.google_calendar_oauth_service import (
    decrypt_google_access_token,
    decrypt_google_refresh_token,
)

logger = logging.getLogger(__name__)


class GoogleCalendarService:
    calendar_base_url = "https://www.googleapis.com/calendar/v3"

    def create_meetup_reminder(
        self,
        user_id: str,
        title: str,
        start_time: str,
        end_time: str,
        location: str = "",
        description: str | None = None,
        reminder_minutes: int = 30,
    ) -> dict[str, Any]:
        clean_user_id = str(user_id or "").strip()
        clean_title = str(title or "").strip()
        clean_start = str(start_time or "").strip()
        clean_end = str(end_time or "").strip()
        clean_location = str(location or "").strip()
        clean_description = str(description or "").strip()
        reminder_value = max(0, int(reminder_minutes or 30))

        if not all([clean_user_id, clean_title, clean_start, clean_end]):
            return {"success": False, "error": "missing_required_fields"}

        profile = AccountProfile.objects.select_related("user").filter(user_id=clean_user_id).first()
        if profile is None:
            return {"success": False, "error": "user_not_found"}

        connection = GoogleCalendarConnection.objects.filter(profile=profile).first()
        if connection is None or not connection.is_connected:
            return {"success": False, "error": "google_calendar_not_connected"}

        access_token = self._get_valid_access_token(connection)
        if not access_token:
            return {"success": False, "error": "google_calendar_not_connected"}

        payload = {
            "summary": clean_title,
            "description": clean_description or "Meetup planned through HelloAgain",
            "start": {"dateTime": clean_start},
            "end": {"dateTime": clean_end},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": reminder_value}],
            },
        }
        if clean_location:
            payload["location"] = clean_location
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{self.calendar_base_url}/calendars/primary/events",
                headers=headers,
                json=payload,
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.warning("google_calendar.create_event_network_error user_id=%s error=%s", clean_user_id, exc)
            return {"success": False, "error": "google_calendar_request_failed"}

        if response.status_code == 401:
            access_token = self._refresh_access_token(connection)
            if not access_token:
                return {"success": False, "error": "google_calendar_not_connected"}
            headers["Authorization"] = f"Bearer {access_token}"
            try:
                response = requests.post(
                    f"{self.calendar_base_url}/calendars/primary/events",
                    headers=headers,
                    json=payload,
                    timeout=20,
                )
            except requests.RequestException as exc:
                logger.warning("google_calendar.create_event_retry_error user_id=%s error=%s", clean_user_id, exc)
                return {"success": False, "error": "google_calendar_request_failed"}

        if response.status_code not in {200, 201}:
            logger.warning(
                "google_calendar.create_event_failed user_id=%s status=%s body=%s",
                clean_user_id,
                response.status_code,
                response.text[:500],
            )
            if response.status_code in {400, 403}:
                return {"success": False, "error": "google_calendar_create_failed"}
            return {"success": False, "error": "google_calendar_request_failed"}

        body = response.json()
        return {
            "success": True,
            "event_id": str(body.get("id") or ""),
            "html_link": str(body.get("htmlLink") or ""),
            "message": "Meetup reminder added to Google Calendar",
        }

    def build_meetup_reminder_payload(
        self,
        *,
        user_id: str,
        friend_name: str,
        start_dt,
        location: str,
        description: str | None = None,
        reminder_minutes: int = 30,
    ) -> dict[str, Any]:
        duration_minutes = int(os.environ.get("GOOGLE_CALENDAR_DEFAULT_MEETUP_DURATION_MINUTES", "60"))
        end_dt = start_dt + timedelta(minutes=max(15, duration_minutes))
        return {
            "user_id": str(user_id),
            "title": f"Meetup with {friend_name}",
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "location": location,
            "description": description or "Meetup planned through HelloAgain",
            "reminder_minutes": reminder_minutes,
        }

    def _get_valid_access_token(self, connection: GoogleCalendarConnection) -> str:
        access_token = decrypt_google_access_token(connection)
        if access_token and not self._token_is_expired(connection):
            return access_token
        return self._refresh_access_token(connection)

    def _token_is_expired(self, connection: GoogleCalendarConnection) -> bool:
        if connection.expires_at is None:
            return not bool(connection.access_token)
        return connection.expires_at <= timezone.now() + timedelta(minutes=2)

    def _refresh_access_token(self, connection: GoogleCalendarConnection) -> str:
        refresh_token = decrypt_google_refresh_token(connection)
        if not refresh_token:
            return ""

        client_id = str(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
        client_secret = str(os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
        if not client_id or not client_secret:
            logger.warning("google_calendar.refresh_missing_client_credentials profile_id=%s", connection.profile_id)
            return ""

        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            response = requests.post(
                connection.token_uri or "https://oauth2.googleapis.com/token",
                data=payload,
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.warning("google_calendar.refresh_network_error profile_id=%s error=%s", connection.profile_id, exc)
            return ""

        if response.status_code != 200:
            logger.warning(
                "google_calendar.refresh_failed profile_id=%s status=%s body=%s",
                connection.profile_id,
                response.status_code,
                response.text[:500],
            )
            return ""

        body = response.json()
        access_token = str(body.get("access_token") or "").strip()
        expires_in = int(body.get("expires_in") or 3600)
        if not access_token:
            return ""

        connection.access_token = encrypt_token(access_token)
        connection.expires_at = timezone.now() + timedelta(seconds=max(60, expires_in))
        connection.save(update_fields=["access_token", "expires_at", "updated_at"])
        return access_token
