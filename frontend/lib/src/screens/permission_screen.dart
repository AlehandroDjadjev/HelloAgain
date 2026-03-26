import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter/material.dart';

import 'home_screen.dart';

/// Shown on first launch if the AccessibilityService isn't enabled.
/// Users can proceed without it — the pipeline will run in "backend-only" mode.
class PermissionScreen extends StatefulWidget {
  const PermissionScreen({super.key});

  @override
  State<PermissionScreen> createState() => _PermissionScreenState();
}

class _PermissionScreenState extends State<PermissionScreen> {
  bool _checking = false;

  Future<void> _checkAndProceed() async {
    setState(() => _checking = true);
    final status = await PermissionChecker.getPermissionStatus();
    if (!mounted) return;
    setState(() => _checking = false);

    final hasAccess = status['accessibilityService'] == true;
    if (hasAccess || true) {
      // Always allow proceeding — service can be enabled later
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => const HomeScreen()),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: cs.surface,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const SizedBox(height: 48),
              Text('HelloAgain',
                  style: Theme.of(context).textTheme.headlineLarge?.copyWith(
                        color: cs.primary,
                        fontWeight: FontWeight.w900,
                        letterSpacing: -1,
                      )),
              const SizedBox(height: 8),
              Text('Text → Gestures Pipeline',
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                        color: cs.onSurface.withAlpha(160),
                      )),
              const Spacer(),
              _PermissionCard(
                icon: Icons.accessibility_new,
                title: 'Accessibility Service',
                description:
                    'Required for executing actions on your device. '
                    'Enable in Android Settings → Accessibility → HelloAgain.',
                onTap: () => PermissionChecker.openAccessibilitySettings(),
              ),
              const SizedBox(height: 16),
              _PermissionCard(
                icon: Icons.layers,
                title: 'Draw over apps',
                description:
                    'Optional — allows the confirmation overlay to appear '
                    'on top of the target app.',
                onTap: () => PermissionChecker.openAccessibilitySettings(),
              ),
              const Spacer(),
              SizedBox(
                width: double.infinity,
                height: 56,
                child: FilledButton(
                  onPressed: _checking ? null : _checkAndProceed,
                  child: _checking
                      ? const SizedBox(
                          width: 24,
                          height: 24,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Text('Continue to Pipeline',
                          style: TextStyle(
                              fontSize: 16, fontWeight: FontWeight.w600)),
                ),
              ),
              const SizedBox(height: 8),
              Center(
                child: TextButton(
                  onPressed: () => Navigator.of(context).pushReplacement(
                    MaterialPageRoute(builder: (_) => const HomeScreen()),
                  ),
                  child: Text(
                    'Skip — backend-only mode',
                    style: TextStyle(color: cs.onSurface.withAlpha(120)),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _PermissionCard extends StatelessWidget {
  const _PermissionCard({
    required this.icon,
    required this.title,
    required this.description,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String description;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Row(
            children: [
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: cs.primaryContainer,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Icon(icon, color: cs.onPrimaryContainer),
              ),
              const SizedBox(width: 16),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(title,
                        style: const TextStyle(
                            fontWeight: FontWeight.w600, fontSize: 15)),
                    const SizedBox(height: 4),
                    Text(description,
                        style: TextStyle(
                            fontSize: 13,
                            color: cs.onSurface.withAlpha(160),
                            height: 1.4)),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Icon(Icons.open_in_new,
                  size: 18, color: cs.onSurface.withAlpha(120)),
            ],
          ),
        ),
      ),
    );
  }
}
