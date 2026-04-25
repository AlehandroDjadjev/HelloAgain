from __future__ import annotations

import logging
import os
import secrets
import urllib.parse
from datetime import timedelta
from typing import Any

import requests
from django.utils import timezone

from .google_calendar_crypto import decrypt_token, encrypt_token
from .models import AccountProfile, GoogleCalendarConnection

logger = logging.getLogger(__name__)


class GoogleCalendarOAuthService:
    auth_base_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    userinfo_url = "https://openidconnect.googleapis.com/v1/userinfo"
    default_scopes = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/calendar.events",
    ]

    def get_connection_status(self, profile: AccountProfile) -> dict[str, Any]:
        connection = GoogleCalendarConnection.objects.filter(profile=profile).first()
        connected = bool(connection and connection.is_connected and connection.is_active)
        return {
            "connected": connected,
            "google_email": str(connection.google_email or "").strip() if connection else "",
        }

    def build_connect_url(self, profile: AccountProfile) -> dict[str, Any]:
        client_id = self._client_id()
        redirect_uri = self._redirect_uri()
        if not client_id or not redirect_uri:
            return {"success": False, "error": "google_oauth_not_configured"}

        connection, _ = GoogleCalendarConnection.objects.get_or_create(profile=profile)
        state = secrets.token_urlsafe(32)
        connection.oauth_state = state
        connection.is_active = False
        connection.save(update_fields=["oauth_state", "is_active", "updated_at"])

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.default_scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return {
            "success": True,
            "auth_url": f"{self.auth_base_url}?{urllib.parse.urlencode(params)}",
        }

    def handle_callback(self, *, state: str, code: str) -> dict[str, Any]:
        clean_state = str(state or "").strip()
        clean_code = str(code or "").strip()
        if not clean_state or not clean_code:
            return {"success": False, "error": "missing_oauth_parameters"}

        connection = GoogleCalendarConnection.objects.select_related("profile", "profile__user").filter(
            oauth_state=clean_state
        ).first()
        if connection is None:
            return {"success": False, "error": "invalid_oauth_state"}

        token_response = self._exchange_code_for_tokens(clean_code)
        if not token_response.get("success"):
            connection.oauth_state = ""
            connection.save(update_fields=["oauth_state", "updated_at"])
            return token_response

        access_token = str(token_response.get("access_token") or "").strip()
        refresh_token = str(token_response.get("refresh_token") or "").strip()
        expires_in = int(token_response.get("expires_in") or 3600)
        scopes = token_response.get("scopes") or self.default_scopes

        google_email = self._fetch_google_email(access_token)

        if refresh_token:
            connection.refresh_token = encrypt_token(refresh_token)
        connection.access_token = encrypt_token(access_token)
        connection.google_email = google_email
        connection.scopes = scopes
        connection.expires_at = timezone.now() + timedelta(seconds=max(60, expires_in))
        connection.oauth_state = ""
        connection.is_active = True
        connection.connected_at = connection.connected_at or timezone.now()
        connection.save(
            update_fields=[
                "access_token",
                "refresh_token",
                "google_email",
                "scopes",
                "expires_at",
                "oauth_state",
                "is_active",
                "connected_at",
                "updated_at",
            ]
        )
        return {"success": True, "google_email": google_email}

    def disconnect(self, profile: AccountProfile) -> dict[str, Any]:
        connection = GoogleCalendarConnection.objects.filter(profile=profile).first()
        if connection is None:
            return {"success": True, "connected": False}
        connection.access_token = ""
        connection.refresh_token = ""
        connection.google_email = ""
        connection.scopes = []
        connection.expires_at = None
        connection.oauth_state = ""
        connection.is_active = False
        connection.save(
            update_fields=[
                "access_token",
                "refresh_token",
                "google_email",
                "scopes",
                "expires_at",
                "oauth_state",
                "is_active",
                "updated_at",
            ]
        )
        return {"success": True, "connected": False}

    def _exchange_code_for_tokens(self, code: str) -> dict[str, Any]:
        client_id = self._client_id()
        client_secret = self._client_secret()
        redirect_uri = self._redirect_uri()
        if not client_id or not client_secret or not redirect_uri:
            return {"success": False, "error": "google_oauth_not_configured"}

        payload = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        try:
            response = requests.post(self.token_url, data=payload, timeout=20)
        except requests.RequestException as exc:
            logger.warning("google_calendar.oauth_token_network_error error=%s", exc)
            return {"success": False, "error": "google_oauth_token_exchange_failed"}

        if response.status_code != 200:
            logger.warning(
                "google_calendar.oauth_token_exchange_failed status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return {"success": False, "error": "google_oauth_token_exchange_failed"}

        body = response.json()
        scope_string = str(body.get("scope") or "").strip()
        return {
            "success": True,
            "access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token"),
            "expires_in": body.get("expires_in"),
            "scopes": scope_string.split() if scope_string else list(self.default_scopes),
        }

    def _fetch_google_email(self, access_token: str) -> str:
        try:
            response = requests.get(
                self.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.warning("google_calendar.userinfo_network_error error=%s", exc)
            return ""
        if response.status_code != 200:
            logger.warning(
                "google_calendar.userinfo_failed status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return ""
        return str(response.json().get("email") or "").strip()

    def _client_id(self) -> str:
        return str(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()

    def _client_secret(self) -> str:
        return str(os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()

    def _redirect_uri(self) -> str:
        return str(os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()


def decrypt_google_access_token(connection: GoogleCalendarConnection) -> str:
    return decrypt_token(connection.access_token)


def decrypt_google_refresh_token(connection: GoogleCalendarConnection) -> str:
    return decrypt_token(connection.refresh_token)
