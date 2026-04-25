import 'package:flutter/material.dart';

class GoogleCalendarConnectView extends StatelessWidget {
  const GoogleCalendarConnectView({
    super.key,
    required this.connected,
    required this.connectedEmail,
    required this.isWorking,
    required this.statusText,
    required this.onConnect,
    required this.onSkip,
    required this.onContinue,
    this.showSkip = true,
  });

  final bool connected;
  final String connectedEmail;
  final bool isWorking;
  final String statusText;
  final Future<void> Function() onConnect;
  final Future<void> Function() onSkip;
  final Future<void> Function() onContinue;
  final bool showSkip;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final subtitle = connected
        ? 'Connected as ${connectedEmail.isEmpty ? 'your Google account' : connectedEmail}.'
        : 'Optional. Add meetups and reminders to your phone calendar automatically.';

    return Scaffold(
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    'Connect Google Calendar?',
                    textAlign: TextAlign.center,
                    style: theme.textTheme.headlineMedium,
                  ),
                  const SizedBox(height: 16),
                  Text(
                    subtitle,
                    textAlign: TextAlign.center,
                    style: theme.textTheme.bodyLarge,
                  ),
                  const SizedBox(height: 20),
                  Text(
                    statusText,
                    textAlign: TextAlign.center,
                    style: theme.textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 28),
                  ElevatedButton(
                    onPressed: isWorking ? null : (connected ? onContinue : onConnect),
                    child: Text(connected ? 'Continue' : 'Connect Google'),
                  ),
                  if (showSkip) ...[
                    const SizedBox(height: 12),
                    OutlinedButton(
                      onPressed: isWorking ? null : onSkip,
                      child: Text(connected ? 'Skip for now' : 'Skip for now'),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
