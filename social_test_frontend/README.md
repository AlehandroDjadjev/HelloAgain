# HelloAgain Social Test

This is a separate Flutter app used only for testing the social + GAT flow.

## What It Tests

- real signup and login
- optional contacts permission and import
- discovery feed from `/api/accounts/discovery/`
- friend requests, accept, decline, cancel
- friends list
- profile privacy gates for phone and email
- tap-to-call and tap-to-email once friendship is accepted

## Run It

1. Start Django:
   - `cd C:\Users\gtxr1\HackTuah12\HelloAgain\backend`
   - `.\venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000`
2. Open this app:
   - `cd C:\Users\gtxr1\HackTuah12\HelloAgain\social_test_frontend`
   - `flutter pub get`
   - `flutter run`

## Backend URL

- Android emulator: `http://10.0.2.2:8000`
- Real phone: use your computer's LAN IP, for example `http://192.168.0.15:8000`

You can change the backend URL from the tune icon on the auth screen or in the signed-in app shell.
