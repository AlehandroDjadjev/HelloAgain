import 'package:flutter/material.dart';
import 'package:flutter_contacts/flutter_contacts.dart';
import 'package:permission_handler/permission_handler.dart' as ph;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final preferences = await SharedPreferences.getInstance();
  final session = SessionController(preferences: preferences);
  await session.bootstrap();
  runApp(SocialTestApp(session: session));
}

class SocialTestApp extends StatelessWidget {
  const SocialTestApp({super.key, required this.session});

  final SessionController session;

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: session,
      builder: (context, _) {
        return MaterialApp(
          debugShowCheckedModeBanner: false,
          title: 'HelloAgain Social Test',
          theme: ThemeData(
            useMaterial3: true,
            colorScheme: ColorScheme.fromSeed(
              seedColor: const Color(0xFF0F766E),
              secondary: const Color(0xFFEA580C),
            ),
            scaffoldBackgroundColor: const Color(0xFFF4F5EF),
            inputDecorationTheme: InputDecorationTheme(
              filled: true,
              fillColor: Colors.white.withValues(alpha: 0.92),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(18),
                borderSide: BorderSide.none,
              ),
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 18,
                vertical: 16,
              ),
            ),
            cardTheme: CardThemeData(
              color: Colors.white.withValues(alpha: 0.94),
              elevation: 0,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(24),
              ),
            ),
          ),
          home: RootGate(session: session),
        );
      },
    );
  }
}

class RootGate extends StatelessWidget {
  const RootGate({super.key, required this.session});

  final SessionController session;

  @override
  Widget build(BuildContext context) {
    if (session.isBootstrapping) {
      return const SplashPage();
    }
    if (session.me == null) {
      return AuthPage(session: session);
    }
    if (!session.contactsPromptSeen) {
      return ContactsOnboardingPage(session: session);
    }
    return AppShell(session: session);
  }
}

class SplashPage extends StatelessWidget {
  const SplashPage({super.key});

  @override
  Widget build(BuildContext context) {
    return AppBackdrop(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 88,
                height: 88,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.9),
                  borderRadius: BorderRadius.circular(28),
                ),
                child: const Icon(Icons.hub_rounded, size: 44),
              ),
              const SizedBox(height: 18),
              Text(
                'HelloAgain Social Test',
                style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                      fontWeight: FontWeight.w800,
                      color: const Color(0xFF0F172A),
                    ),
              ),
              const SizedBox(height: 10),
              const CircularProgressIndicator(),
            ],
          ),
        ),
      ),
    );
  }
}

enum AuthMode { login, signUp }

class AuthPage extends StatefulWidget {
  const AuthPage({super.key, required this.session});

  final SessionController session;

  @override
  State<AuthPage> createState() => _AuthPageState();
}

class _AuthPageState extends State<AuthPage> {
  AuthMode _mode = AuthMode.signUp;
  bool _submitting = false;
  String? _generalError;
  Map<String, List<String>> _fieldErrors = const <String, List<String>>{};

  final _identifierController = TextEditingController();
  final _loginPasswordController = TextEditingController();
  final _usernameController = TextEditingController();
  final _displayNameController = TextEditingController();
  final _emailController = TextEditingController();
  final _signupPasswordController = TextEditingController();
  final _phoneController = TextEditingController();
  final _descriptionController = TextEditingController();

  @override
  void dispose() {
    _identifierController.dispose();
    _loginPasswordController.dispose();
    _usernameController.dispose();
    _displayNameController.dispose();
    _emailController.dispose();
    _signupPasswordController.dispose();
    _phoneController.dispose();
    _descriptionController.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() {
      _submitting = true;
      _generalError = null;
      _fieldErrors = const <String, List<String>>{};
    });

    try {
      if (_mode == AuthMode.login) {
        await widget.session.login(
          identifier: _identifierController.text.trim(),
          password: _loginPasswordController.text,
        );
      } else {
        await widget.session.register(
          username: _usernameController.text.trim(),
          displayName: _displayNameController.text.trim(),
          email: _emailController.text.trim(),
          password: _signupPasswordController.text,
          phoneNumber: _phoneController.text.trim(),
          description: _descriptionController.text.trim(),
        );
        await _requestLocationPermissionAfterSignUp();
      }
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _generalError = error.message;
        _fieldErrors = error.fieldErrors;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _generalError =
            'The app could not reach the backend. Check the saved backend URL and make sure Django is running.';
      });
    } finally {
      if (mounted) {
        setState(() {
          _submitting = false;
        });
      }
    }
  }

  Future<void> _requestLocationPermissionAfterSignUp() async {
    final status = await ph.Permission.location.request();
    if (!mounted) {
      return;
    }

    final messenger = ScaffoldMessenger.of(context);
    if (status == ph.PermissionStatus.granted || status == ph.PermissionStatus.limited) {
      messenger.showSnackBar(
        const SnackBar(content: Text('Location permission granted.')),
      );
      return;
    }

    if (status == ph.PermissionStatus.permanentlyDenied ||
        status == ph.PermissionStatus.restricted) {
      messenger.showSnackBar(
        SnackBar(
          content: const Text('Location permission is blocked. Enable it in app settings.'),
          action: SnackBarAction(
            label: 'Settings',
            onPressed: ph.openAppSettings,
          ),
        ),
      );
      return;
    }

    messenger.showSnackBar(
      const SnackBar(
        content: Text('Location permission not granted. You can enable it later from settings.'),
      ),
    );
  }

  String? _errorFor(String field) {
    final messages = _fieldErrors[field];
    if (messages == null || messages.isEmpty) {
      return null;
    }
    return messages.join('\n');
  }

  @override
  Widget build(BuildContext context) {
    return AppBackdrop(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(20),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520),
                child: Card(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Container(
                              padding: const EdgeInsets.all(12),
                              decoration: BoxDecoration(
                                color: const Color(0xFF0F766E).withValues(alpha: 0.12),
                                borderRadius: BorderRadius.circular(18),
                              ),
                              child: const Icon(Icons.people_alt_rounded),
                            ),
                            const SizedBox(width: 14),
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    'HelloAgain Social Test',
                                    style: Theme.of(context)
                                        .textTheme
                                        .headlineSmall
                                        ?.copyWith(fontWeight: FontWeight.w800),
                                  ),
                                  const SizedBox(height: 4),
                                  Text(
                                    'Register, match, send requests, accept, then unlock contact details.',
                                    style: Theme.of(context).textTheme.bodyMedium,
                                  ),
                                ],
                              ),
                            ),
                            IconButton(
                              tooltip: 'Backend settings',
                              onPressed: () => showBackendSettingsSheet(
                                context,
                                session: widget.session,
                              ),
                              icon: const Icon(Icons.tune_rounded),
                            ),
                          ],
                        ),
                        const SizedBox(height: 24),
                        SegmentedButton<AuthMode>(
                          segments: const [
                            ButtonSegment<AuthMode>(
                              value: AuthMode.login,
                              label: Text('Login'),
                              icon: Icon(Icons.login_rounded),
                            ),
                            ButtonSegment<AuthMode>(
                              value: AuthMode.signUp,
                              label: Text('Sign Up'),
                              icon: Icon(Icons.person_add_alt_1_rounded),
                            ),
                          ],
                          selected: <AuthMode>{_mode},
                          onSelectionChanged: (selection) {
                            setState(() {
                              _mode = selection.first;
                              _generalError = null;
                              _fieldErrors = const <String, List<String>>{};
                            });
                          },
                        ),
                        const SizedBox(height: 20),
                        if (_generalError != null) ...[
                          ErrorBanner(message: _generalError!),
                          const SizedBox(height: 16),
                        ],
                        if (_mode == AuthMode.login) ...[
                          TextField(
                            controller: _identifierController,
                            decoration: InputDecoration(
                              labelText: 'Username or email',
                              errorText: _errorFor('identifier'),
                            ),
                          ),
                          const SizedBox(height: 14),
                          TextField(
                            controller: _loginPasswordController,
                            obscureText: true,
                            decoration: InputDecoration(
                              labelText: 'Password',
                              errorText: _errorFor('password'),
                            ),
                          ),
                        ] else ...[
                          TextField(
                            controller: _usernameController,
                            decoration: InputDecoration(
                              labelText: 'Username',
                              errorText: _errorFor('username'),
                            ),
                          ),
                          const SizedBox(height: 14),
                          TextField(
                            controller: _displayNameController,
                            decoration: InputDecoration(
                              labelText: 'Display name',
                              errorText: _errorFor('display_name'),
                            ),
                          ),
                          const SizedBox(height: 14),
                          TextField(
                            controller: _emailController,
                            keyboardType: TextInputType.emailAddress,
                            decoration: InputDecoration(
                              labelText: 'Email',
                              errorText: _errorFor('email'),
                            ),
                          ),
                          const SizedBox(height: 14),
                          TextField(
                            controller: _signupPasswordController,
                            obscureText: true,
                            decoration: InputDecoration(
                              labelText: 'Password',
                              helperText:
                                  'Django password rules stay on, so use a strong password.',
                              errorText: _errorFor('password'),
                            ),
                          ),
                          const SizedBox(height: 14),
                          TextField(
                            controller: _phoneController,
                            keyboardType: TextInputType.phone,
                            decoration: InputDecoration(
                              labelText: 'Phone number',
                              errorText: _errorFor('phone_number'),
                            ),
                          ),
                          const SizedBox(height: 14),
                          TextField(
                            controller: _descriptionController,
                            minLines: 4,
                            maxLines: 6,
                            decoration: InputDecoration(
                              labelText: 'Description',
                              alignLabelWithHint: true,
                              helperText:
                                  'This is the most important field for the first GAT test.',
                              errorText: _errorFor('description'),
                            ),
                          ),
                        ],
                        const SizedBox(height: 22),
                        FilledButton.icon(
                          onPressed: _submitting ? null : _submit,
                          icon: _submitting
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(strokeWidth: 2),
                                )
                              : Icon(
                                  _mode == AuthMode.login
                                      ? Icons.login_rounded
                                      : Icons.arrow_forward_rounded,
                                ),
                          label: Text(
                            _mode == AuthMode.login ? 'Log In' : 'Create Account',
                          ),
                        ),
                        const SizedBox(height: 14),
                        Text(
                          'Backend: ${widget.session.baseUrl}',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class ContactsOnboardingPage extends StatefulWidget {
  const ContactsOnboardingPage({super.key, required this.session});

  final SessionController session;

  @override
  State<ContactsOnboardingPage> createState() => _ContactsOnboardingPageState();
}

class _ContactsOnboardingPageState extends State<ContactsOnboardingPage> {
  bool _working = false;
  String? _message;
  bool _showSettings = false;

  Future<void> _continueWithoutContacts() async {
    setState(() {
      _working = true;
      _message = null;
    });
    try {
      await widget.session.updateMe(
        <String, dynamic>{'contacts_permission_granted': false},
      );
    } catch (_) {
      // Continue even if the backend update fails.
    } finally {
      await widget.session.markContactsPromptSeen();
      if (mounted) {
        setState(() {
          _working = false;
        });
      }
    }
  }

  Future<void> _syncContacts() async {
    setState(() {
      _working = true;
      _message = null;
      _showSettings = false;
    });

    try {
      final result = await importContactsFromDevice(widget.session);
      if (!mounted) {
        return;
      }
      setState(() {
        _message = result.message;
        _showSettings = result.permanentlyDenied;
      });
      await widget.session.markContactsPromptSeen();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _message = error.message;
      });
      await widget.session.markContactsPromptSeen();
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _message =
            'Contacts sync hit a problem, but you can keep going and test matching anyway.';
      });
      await widget.session.markContactsPromptSeen();
    } finally {
      if (mounted) {
        setState(() {
          _working = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return AppBackdrop(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 520),
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Card(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Container(
                          padding: const EdgeInsets.all(14),
                          decoration: BoxDecoration(
                            color: const Color(0xFFEA580C).withValues(alpha: 0.12),
                            borderRadius: BorderRadius.circular(20),
                          ),
                          child: const Icon(Icons.contact_phone_rounded, size: 34),
                        ),
                        const SizedBox(height: 18),
                        Text(
                          'Optional contacts sync',
                          style: Theme.of(context)
                              .textTheme
                              .headlineSmall
                              ?.copyWith(fontWeight: FontWeight.w800),
                        ),
                        const SizedBox(height: 10),
                        Text(
                          'Granting contacts is optional. It helps match people you already know, but the GAT and friend request flow still works if you skip it.',
                          style: Theme.of(context).textTheme.bodyLarge,
                        ),
                        const SizedBox(height: 20),
                        if (_message != null) ...[
                          InfoBanner(message: _message!),
                          const SizedBox(height: 16),
                        ],
                        FilledButton.icon(
                          onPressed: _working ? null : _syncContacts,
                          icon: _working
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(strokeWidth: 2),
                                )
                              : const Icon(Icons.download_for_offline_rounded),
                          label: const Text('Allow and sync contacts'),
                        ),
                        const SizedBox(height: 12),
                        OutlinedButton(
                          onPressed: _working ? null : _continueWithoutContacts,
                          child: const Text('Skip for now'),
                        ),
                        if (_showSettings) ...[
                          const SizedBox(height: 12),
                          TextButton.icon(
                            onPressed: ph.openAppSettings,
                            icon: const Icon(Icons.settings_rounded),
                            label: const Text('Open device settings'),
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class AppShell extends StatefulWidget {
  const AppShell({super.key, required this.session});

  final SessionController session;

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  int _index = 0;
  int _refreshToken = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _requestLocationPermissionOnEntry();
    });
  }

  Future<void> _requestLocationPermissionOnEntry() async {
    final currentStatus = await ph.Permission.location.status;
    if (!mounted) {
      return;
    }

    if (currentStatus == ph.PermissionStatus.granted ||
        currentStatus == ph.PermissionStatus.limited) {
      return;
    }

    final requestedStatus = currentStatus == ph.PermissionStatus.denied
        ? await ph.Permission.location.request()
        : currentStatus;

    if (!mounted) {
      return;
    }

    final messenger = ScaffoldMessenger.of(context);
    if (requestedStatus == ph.PermissionStatus.granted ||
        requestedStatus == ph.PermissionStatus.limited) {
      messenger.showSnackBar(
        const SnackBar(content: Text('Location permission granted.')),
      );
      return;
    }

    if (requestedStatus == ph.PermissionStatus.permanentlyDenied ||
        requestedStatus == ph.PermissionStatus.restricted) {
      messenger.showSnackBar(
        SnackBar(
          content: const Text('Location permission is blocked. Enable it in app settings.'),
          action: SnackBarAction(
            label: 'Settings',
            onPressed: ph.openAppSettings,
          ),
        ),
      );
      return;
    }

    messenger.showSnackBar(
      const SnackBar(
        content: Text('Location permission not granted. You can enable it later from settings.'),
      ),
    );
  }

  void _refreshAll() {
    setState(() {
      _refreshToken++;
    });
  }

  Future<void> _openUserProfile(AppProfile profile) async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => UserProfilePage(
          session: widget.session,
          initialProfile: profile,
          onSocialChange: _refreshAll,
          onOpenRequests: () {
            setState(() {
              _index = 1;
              _refreshToken++;
            });
          },
        ),
      ),
    );
    _refreshAll();
  }

  @override
  Widget build(BuildContext context) {
    final me = widget.session.me!;
    const titles = ['Discover', 'Requests', 'Friends', 'Profile'];

    return AppBackdrop(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        appBar: AppBar(
          backgroundColor: Colors.transparent,
          elevation: 0,
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(titles[_index]),
              Text(
                me.displayName,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ),
          actions: [
            IconButton(
              tooltip: 'Refresh',
              onPressed: _refreshAll,
              icon: const Icon(Icons.refresh_rounded),
            ),
            IconButton(
              tooltip: 'Backend settings',
              onPressed: () => showBackendSettingsSheet(
                context,
                session: widget.session,
              ),
              icon: const Icon(Icons.tune_rounded),
            ),
          ],
        ),
        body: IndexedStack(
          index: _index,
          children: [
            HomeTab(
              session: widget.session,
              refreshToken: _refreshToken,
              onOpenProfile: _openUserProfile,
              onOpenRequests: () => setState(() => _index = 1),
              onSocialChange: _refreshAll,
            ),
            RequestsTab(
              session: widget.session,
              refreshToken: _refreshToken,
              onOpenProfile: _openUserProfile,
              onSocialChange: _refreshAll,
            ),
            FriendsTab(
              session: widget.session,
              refreshToken: _refreshToken,
              onOpenProfile: _openUserProfile,
              onSocialChange: _refreshAll,
            ),
            ProfileTab(
              session: widget.session,
              onContactsSync: () async {
                final messenger = ScaffoldMessenger.of(context);
                final result = await importContactsFromDevice(widget.session);
                if (!mounted) {
                  return;
                }
                messenger.showSnackBar(
                  SnackBar(content: Text(result.message)),
                );
                _refreshAll();
              },
            ),
          ],
        ),
        bottomNavigationBar: Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(28),
            child: NavigationBar(
              selectedIndex: _index,
              onDestinationSelected: (value) {
                setState(() {
                  _index = value;
                });
              },
              destinations: const [
                NavigationDestination(
                  icon: Icon(Icons.travel_explore_rounded),
                  label: 'Home',
                ),
                NavigationDestination(
                  icon: Icon(Icons.mark_email_unread_rounded),
                  label: 'Requests',
                ),
                NavigationDestination(
                  icon: Icon(Icons.groups_rounded),
                  label: 'Friends',
                ),
                NavigationDestination(
                  icon: Icon(Icons.account_circle_rounded),
                  label: 'Profile',
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

enum DiscoveryModeChoice { forYou, describeSomeone }

class HomeTab extends StatefulWidget {
  const HomeTab({
    super.key,
    required this.session,
    required this.refreshToken,
    required this.onOpenProfile,
    required this.onOpenRequests,
    required this.onSocialChange,
  });

  final SessionController session;
  final int refreshToken;
  final ValueChanged<AppProfile> onOpenProfile;
  final VoidCallback onOpenRequests;
  final VoidCallback onSocialChange;

  @override
  State<HomeTab> createState() => _HomeTabState();
}

class _HomeTabState extends State<HomeTab> {
  final _searchController = TextEditingController();
  final _descriptionController = TextEditingController();
  DiscoveryModeChoice _mode = DiscoveryModeChoice.forYou;
  int _loadGeneration = 0;
  bool _loading = true;
  String? _error;
  List<AppProfile> _profiles = const <AppProfile>[];

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant HomeTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.refreshToken != widget.refreshToken) {
      _load();
    }
  }

  @override
  void dispose() {
    _searchController.dispose();
    _descriptionController.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final generation = ++_loadGeneration;
    final modeSnapshot = _mode;
    final searchSnapshot = _searchController.text;
    final descriptionSnapshot = _descriptionController.text.trim();

    setState(() {
      _loading = true;
      _error = null;
      _profiles = const <AppProfile>[];
    });
    if (modeSnapshot == DiscoveryModeChoice.describeSomeone &&
        descriptionSnapshot.isEmpty) {
      setState(() {
        _profiles = const <AppProfile>[];
        _loading = false;
        _error = 'Write a short description of the kind of person you want to meet.';
      });
      return;
    }
    try {
      final profiles = modeSnapshot == DiscoveryModeChoice.forYou
          ? await widget.session.api.fetchDiscovery(
              query: searchSnapshot,
            )
          : await widget.session.api.fetchDescriptionDiscovery(
              description: descriptionSnapshot,
              limit: 8,
            );
      if (!mounted || generation != _loadGeneration) {
        return;
      }
      setState(() {
        _profiles = profiles;
      });
    } on ApiException catch (error) {
      if (!mounted || generation != _loadGeneration) {
        return;
      }
      setState(() {
        _error = error.message;
      });
    } catch (_) {
      if (!mounted || generation != _loadGeneration) {
        return;
      }
      setState(() {
        _error = 'Discovery is unavailable right now.';
      });
    } finally {
      if (mounted && generation == _loadGeneration) {
        setState(() {
          _loading = false;
        });
      }
    }
  }

  Future<void> _openProfileFromResults(AppProfile profile) async {
    final eventType = _mode == DiscoveryModeChoice.forYou
        ? 'recommendation_clicked'
        : 'search_result_opened';
    final discoveryMode =
        _mode == DiscoveryModeChoice.forYou ? 'for_you' : 'describe_someone';
    try {
      await widget.session.api.logActivity(
        eventType: eventType,
        targetUserId: profile.userId,
        discoveryMode: discoveryMode,
        queryText: _mode == DiscoveryModeChoice.describeSomeone
            ? _descriptionController.text.trim()
            : _searchController.text.trim(),
        metadata: const {'surface': 'home_results'},
      );
    } catch (_) {
      // Keep profile navigation resilient.
    }
    widget.onOpenProfile(profile);
  }

  Future<void> _sendRequest(AppProfile profile) async {
    try {
      await widget.session.api.sendFriendRequest(targetUserId: profile.userId);
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Friend request sent to ${profile.displayName}.')),
      );
      widget.onSocialChange();
      await _load();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(error.message)),
      );
    }
  }

  Widget _buildAction(AppProfile profile) {
    switch (profile.friendStatus) {
      case 'none':
        return FilledButton(
          onPressed: () => _sendRequest(profile),
          child: const Text('Send request'),
        );
      case 'incoming_pending':
        return OutlinedButton(
          onPressed: widget.onOpenRequests,
          child: const Text('Open request'),
        );
      case 'outgoing_pending':
        return const StatusChip(label: 'Pending');
      case 'accepted':
        return const StatusChip(label: 'Friends');
      default:
        return const SizedBox.shrink();
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'Discovery feed',
                    style: Theme.of(context)
                        .textTheme
                        .titleLarge
                        ?.copyWith(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    _mode == DiscoveryModeChoice.forYou
                        ? 'Graph-first friend discovery using your account profile, social edges, and recent activity.'
                        : 'Describe the kind of person you want to meet and the backend will rank real users against that target persona.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 14),
                  SegmentedButton<DiscoveryModeChoice>(
                    segments: const [
                      ButtonSegment<DiscoveryModeChoice>(
                        value: DiscoveryModeChoice.forYou,
                        label: Text('For You'),
                        icon: Icon(Icons.auto_awesome_rounded),
                      ),
                      ButtonSegment<DiscoveryModeChoice>(
                        value: DiscoveryModeChoice.describeSomeone,
                        label: Text('Describe Someone'),
                        icon: Icon(Icons.edit_note_rounded),
                      ),
                    ],
                    selected: <DiscoveryModeChoice>{_mode},
                    onSelectionChanged: (selection) {
                      setState(() {
                        _mode = selection.first;
                      });
                      _load();
                    },
                  ),
                  const SizedBox(height: 14),
                  if (_mode == DiscoveryModeChoice.forYou)
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: _searchController,
                            onSubmitted: (_) => _load(),
                            decoration: const InputDecoration(
                              labelText: 'Filter by name or interests',
                              prefixIcon: Icon(Icons.search_rounded),
                            ),
                          ),
                        ),
                        const SizedBox(width: 10),
                        FilledButton(
                          onPressed: _load,
                          child: const Text('Refresh'),
                        ),
                      ],
                    )
                  else
                    Column(
                      children: [
                        TextField(
                          controller: _descriptionController,
                          minLines: 3,
                          maxLines: 5,
                          onSubmitted: (_) => _load(),
                          decoration: const InputDecoration(
                            labelText: 'Describe the kind of person you want to meet',
                            alignLabelWithHint: true,
                            prefixIcon: Icon(Icons.psychology_alt_rounded),
                          ),
                        ),
                        const SizedBox(height: 10),
                        Align(
                          alignment: Alignment.centerRight,
                          child: FilledButton(
                            onPressed: _load,
                            child: const Text('Find matches'),
                          ),
                        ),
                      ],
                    ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          if (_loading && _profiles.isEmpty)
            const Center(
              child: Padding(
                padding: EdgeInsets.only(top: 48),
                child: CircularProgressIndicator(),
              ),
            )
          else if (_error != null && _profiles.isEmpty)
            ErrorCard(message: _error!)
          else if (_profiles.isEmpty)
            const EmptyCard(
              title: 'No matches yet',
              message:
                  'Try another search term or register another user with a richer description.',
            )
          else
            ..._profiles.map(
              (profile) => Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: PersonCard(
                  profile: profile,
                  onTap: () => _openProfileFromResults(profile),
                  action: _buildAction(profile),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class RequestsTab extends StatefulWidget {
  const RequestsTab({
    super.key,
    required this.session,
    required this.refreshToken,
    required this.onOpenProfile,
    required this.onSocialChange,
  });

  final SessionController session;
  final int refreshToken;
  final ValueChanged<AppProfile> onOpenProfile;
  final VoidCallback onSocialChange;

  @override
  State<RequestsTab> createState() => _RequestsTabState();
}

class _RequestsTabState extends State<RequestsTab> {
  bool _loading = true;
  String? _error;
  FriendRequestBucket _bucket =
      const FriendRequestBucket(incoming: <FriendRequestRow>[], outgoing: <FriendRequestRow>[]);
  MeetupInviteBucket _meetupBucket =
      const MeetupInviteBucket(incoming: <MeetupInviteRow>[], outgoing: <MeetupInviteRow>[]);

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant RequestsTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.refreshToken != widget.refreshToken) {
      _load();
    }
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final results = await Future.wait<dynamic>([
        widget.session.api.fetchFriendRequests(),
        widget.session.api.fetchMeetupInvites(),
      ]);
      final bucket = results[0] as FriendRequestBucket;
      final meetupBucket = results[1] as MeetupInviteBucket;
      if (!mounted) {
        return;
      }
      setState(() {
        _bucket = bucket;
        _meetupBucket = meetupBucket;
      });
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _error = error.message;
      });
    } finally {
      if (mounted) {
        setState(() {
          _loading = false;
        });
      }
    }
  }

  Future<void> _respondMeetup(MeetupInviteRow row, String action) async {
    try {
      await widget.session.api.respondMeetupInvite(inviteId: row.id, action: action);
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Meetup invite ${action}ed.')),
      );
      widget.onSocialChange();
      await _load();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(error.message)),
      );
    }
  }

  String _formatInviteTime(DateTime? value) {
    if (value == null) {
      return 'time not set';
    }
    final local = value.toLocal();
    final month = local.month.toString().padLeft(2, '0');
    final day = local.day.toString().padLeft(2, '0');
    final hour = local.hour.toString().padLeft(2, '0');
    final minute = local.minute.toString().padLeft(2, '0');
    return '$day.$month $hour:$minute';
  }

  Future<void> _respond(FriendRequestRow row, String action) async {
    try {
      await widget.session.api.respondToFriendRequest(
        requestId: row.id,
        action: action,
      );
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Request ${action}ed.')),
      );
      widget.onSocialChange();
      await _load();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(error.message)),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
        children: [
          if (_loading &&
              _bucket.incoming.isEmpty &&
              _bucket.outgoing.isEmpty &&
              _meetupBucket.incoming.isEmpty &&
              _meetupBucket.outgoing.isEmpty)
            const Center(
              child: Padding(
                padding: EdgeInsets.only(top: 48),
                child: CircularProgressIndicator(),
              ),
            )
          else if (_error != null &&
              _bucket.incoming.isEmpty &&
              _bucket.outgoing.isEmpty &&
              _meetupBucket.incoming.isEmpty &&
              _meetupBucket.outgoing.isEmpty)
            ErrorCard(message: _error!)
          else if (_bucket.incoming.isEmpty &&
              _bucket.outgoing.isEmpty &&
              _meetupBucket.incoming.isEmpty &&
              _meetupBucket.outgoing.isEmpty)
            const EmptyCard(
              title: 'No requests yet',
              message:
                  'Send one from Home, then switch users and accept it here.',
            )
          else ...[
            if (_bucket.incoming.isNotEmpty) ...[
              const SectionHeader(title: 'Incoming'),
              ..._bucket.incoming.map(
                (row) => Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: RequestCard(
                    row: row,
                    onTap: () => widget.onOpenProfile(row.counterparty),
                    trailing: row.status == 'pending'
                        ? Wrap(
                            spacing: 8,
                            children: [
                              FilledButton(
                                onPressed: () => _respond(row, 'accept'),
                                child: const Text('Accept'),
                              ),
                              OutlinedButton(
                                onPressed: () => _respond(row, 'decline'),
                                child: const Text('Decline'),
                              ),
                            ],
                          )
                        : StatusChip(label: row.status),
                  ),
                ),
              ),
            ],
            if (_bucket.outgoing.isNotEmpty) ...[
              const SectionHeader(title: 'Outgoing'),
              ..._bucket.outgoing.map(
                (row) => Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: RequestCard(
                    row: row,
                    onTap: () => widget.onOpenProfile(row.counterparty),
                    trailing: row.status == 'pending'
                        ? OutlinedButton(
                            onPressed: () => _respond(row, 'cancel'),
                            child: const Text('Cancel'),
                          )
                        : StatusChip(label: row.status),
                  ),
                ),
              ),
            ],
            if (_meetupBucket.incoming.isNotEmpty) ...[
              const SectionHeader(title: 'Meetup Invites (Incoming)'),
              ..._meetupBucket.incoming.map(
                (row) => Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Card(
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${row.requesterDisplayName} invited you',
                            style: Theme.of(context)
                                .textTheme
                                .titleMedium
                                ?.copyWith(fontWeight: FontWeight.w700),
                          ),
                          const SizedBox(height: 8),
                          Text('Place: ${row.placeName}'),
                          Text('When: ${_formatInviteTime(row.proposedTime)}'),
                          if (row.weather.isNotEmpty) Text('Weather: ${row.weather}'),
                          const SizedBox(height: 10),
                          row.status == 'pending'
                              ? Wrap(
                                  spacing: 8,
                                  children: [
                                    FilledButton(
                                      onPressed: () => _respondMeetup(row, 'accept'),
                                      child: const Text('Accept'),
                                    ),
                                    OutlinedButton(
                                      onPressed: () => _respondMeetup(row, 'decline'),
                                      child: const Text('Decline'),
                                    ),
                                  ],
                                )
                              : StatusChip(label: row.status),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ],
            if (_meetupBucket.outgoing.isNotEmpty) ...[
              const SectionHeader(title: 'Meetup Invites (Outgoing)'),
              ..._meetupBucket.outgoing.map(
                (row) => Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Card(
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Invite to ${row.invitedDisplayName}',
                            style: Theme.of(context)
                                .textTheme
                                .titleMedium
                                ?.copyWith(fontWeight: FontWeight.w700),
                          ),
                          const SizedBox(height: 8),
                          Text('Place: ${row.placeName}'),
                          Text('When: ${_formatInviteTime(row.proposedTime)}'),
                          const SizedBox(height: 10),
                          row.status == 'pending'
                              ? OutlinedButton(
                                  onPressed: () => _respondMeetup(row, 'cancel'),
                                  child: const Text('Cancel'),
                                )
                              : StatusChip(label: row.status),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ],
        ],
      ),
    );
  }
}

class FriendsTab extends StatefulWidget {
  const FriendsTab({
    super.key,
    required this.session,
    required this.refreshToken,
    required this.onOpenProfile,
    required this.onSocialChange,
  });

  final SessionController session;
  final int refreshToken;
  final ValueChanged<AppProfile> onOpenProfile;
  final VoidCallback onSocialChange;

  @override
  State<FriendsTab> createState() => _FriendsTabState();
}

class _FriendsTabState extends State<FriendsTab> {
  bool _loading = true;
  String? _error;
  List<AppProfile> _friends = const <AppProfile>[];

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(covariant FriendsTab oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.refreshToken != widget.refreshToken) {
      _load();
    }
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final friends = await widget.session.api.fetchFriends();
      if (!mounted) {
        return;
      }
      setState(() {
        _friends = friends;
      });
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _error = error.message;
      });
    } finally {
      if (mounted) {
        setState(() {
          _loading = false;
        });
      }
    }
  }

  Future<void> _proposeMeetup(AppProfile friend) async {
    try {
      await widget.session.api.proposeMeetup(friend: friend, me: widget.session.me);
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Meetup invitation sent to ${friend.displayName}.')),
      );
      widget.onSocialChange();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(error.message)),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
        children: [
          if (_loading && _friends.isEmpty)
            const Center(
              child: Padding(
                padding: EdgeInsets.only(top: 48),
                child: CircularProgressIndicator(),
              ),
            )
          else if (_error != null && _friends.isEmpty)
            ErrorCard(message: _error!)
          else if (_friends.isEmpty)
            const EmptyCard(
              title: 'No friends yet',
              message: 'Accepted requests will show up here.',
            )
          else
            ..._friends.map(
              (friend) => Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: PersonCard(
                  profile: friend,
                  onTap: () => widget.onOpenProfile(friend),
                  action: Wrap(
                    spacing: 8,
                    children: [
                      if ((friend.phoneNumber ?? '').isNotEmpty)
                        IconButton.filledTonal(
                          onPressed: () => launchExternalUri('tel:${friend.phoneNumber}'),
                          icon: const Icon(Icons.call_rounded),
                        ),
                      if ((friend.email ?? '').isNotEmpty)
                        IconButton.filledTonal(
                          onPressed: () => launchExternalUri('mailto:${friend.email}'),
                          icon: const Icon(Icons.email_rounded),
                        ),
                      FilledButton.tonalIcon(
                        onPressed: () => _proposeMeetup(friend),
                        icon: const Icon(Icons.place_rounded),
                        label: const Text('Propose meetup'),
                      ),
                    ],
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class ProfileTab extends StatefulWidget {
  const ProfileTab({
    super.key,
    required this.session,
    required this.onContactsSync,
  });

  final SessionController session;
  final Future<void> Function() onContactsSync;

  @override
  State<ProfileTab> createState() => _ProfileTabState();
}

class _ProfileTabState extends State<ProfileTab> {
  late final TextEditingController _displayNameController;
  late final TextEditingController _phoneController;
  late final TextEditingController _descriptionController;
  late final TextEditingController _homeLatController;
  late final TextEditingController _homeLngController;
  late final TextEditingController _goalController;
  late final TextEditingController _vibeController;

  bool _sharePhone = true;
  bool _shareEmail = true;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    final me = widget.session.me!;
    _displayNameController = TextEditingController(text: me.displayName);
    _phoneController = TextEditingController(text: me.phoneNumber ?? '');
    _descriptionController = TextEditingController(text: me.description);
    _homeLatController = TextEditingController(
      text: me.homeLat?.toString() ?? '',
    );
    _homeLngController = TextEditingController(
      text: me.homeLng?.toString() ?? '',
    );
    _goalController = TextEditingController(
      text: me.onboardingAnswers['friendship_goal'] ?? '',
    );
    _vibeController = TextEditingController(
      text: me.onboardingAnswers['conversation_vibe'] ?? '',
    );
    _sharePhone = me.sharePhoneWithFriends;
    _shareEmail = me.shareEmailWithFriends;
  }

  @override
  void dispose() {
    _displayNameController.dispose();
    _phoneController.dispose();
    _descriptionController.dispose();
    _homeLatController.dispose();
    _homeLngController.dispose();
    _goalController.dispose();
    _vibeController.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final parsedLat = double.tryParse(_homeLatController.text.trim());
    final parsedLng = double.tryParse(_homeLngController.text.trim());

    if (_homeLatController.text.trim().isNotEmpty && parsedLat == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Home latitude must be a number.')),
      );
      return;
    }
    if (_homeLngController.text.trim().isNotEmpty && parsedLng == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Home longitude must be a number.')),
      );
      return;
    }

    setState(() {
      _saving = true;
    });
    try {
      await widget.session.updateMe(
        <String, dynamic>{
          'display_name': _displayNameController.text.trim(),
          'phone_number': _phoneController.text.trim(),
          'description': _descriptionController.text.trim(),
          if (_homeLatController.text.trim().isNotEmpty) 'home_lat': parsedLat,
          if (_homeLngController.text.trim().isNotEmpty) 'home_lng': parsedLng,
          'share_phone_with_friends': _sharePhone,
          'share_email_with_friends': _shareEmail,
          'onboarding_answers': <String, dynamic>{
            'friendship_goal': _goalController.text.trim(),
            'conversation_vibe': _vibeController.text.trim(),
          },
        },
      );
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Profile saved.')),
      );
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(error.message)),
      );
    } finally {
      if (mounted) {
        setState(() {
          _saving = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final me = widget.session.me!;
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  me.username,
                  style: Theme.of(context)
                      .textTheme
                      .headlineSmall
                      ?.copyWith(fontWeight: FontWeight.w800),
                ),
                const SizedBox(height: 6),
                Text(me.email ?? 'No email available'),
                const SizedBox(height: 18),
                TextField(
                  controller: _displayNameController,
                  decoration: const InputDecoration(labelText: 'Display name'),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _phoneController,
                  keyboardType: TextInputType.phone,
                  decoration: const InputDecoration(labelText: 'Phone number'),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _descriptionController,
                  minLines: 4,
                  maxLines: 6,
                  decoration: const InputDecoration(
                    labelText: 'Description',
                    alignLabelWithHint: true,
                  ),
                ),
                const SizedBox(height: 14),
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _homeLatController,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                        decoration: const InputDecoration(labelText: 'Home latitude'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: TextField(
                        controller: _homeLngController,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                        decoration: const InputDecoration(labelText: 'Home longitude'),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _goalController,
                  decoration: const InputDecoration(
                    labelText: 'Friendship goal',
                  ),
                ),
                const SizedBox(height: 14),
                TextField(
                  controller: _vibeController,
                  decoration: const InputDecoration(
                    labelText: 'Conversation vibe',
                  ),
                ),
                const SizedBox(height: 14),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Share phone with friends'),
                  value: _sharePhone,
                  onChanged: (value) => setState(() => _sharePhone = value),
                ),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Share email with friends'),
                  value: _shareEmail,
                  onChanged: (value) => setState(() => _shareEmail = value),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 10,
                  runSpacing: 10,
                  children: [
                    FilledButton.icon(
                      onPressed: _saving ? null : _save,
                      icon: _saving
                          ? const SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Icon(Icons.save_rounded),
                      label: const Text('Save profile'),
                    ),
                    OutlinedButton.icon(
                      onPressed: widget.onContactsSync,
                      icon: const Icon(Icons.sync_rounded),
                      label: Text(
                        me.contactsPermissionGranted
                            ? 'Re-sync contacts'
                            : 'Import contacts',
                      ),
                    ),
                    OutlinedButton.icon(
                      onPressed: widget.session.logout,
                      icon: const Icon(Icons.logout_rounded),
                      label: const Text('Logout'),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class UserProfilePage extends StatefulWidget {
  const UserProfilePage({
    super.key,
    required this.session,
    required this.initialProfile,
    required this.onSocialChange,
    required this.onOpenRequests,
  });

  final SessionController session;
  final AppProfile initialProfile;
  final VoidCallback onSocialChange;
  final VoidCallback onOpenRequests;

  @override
  State<UserProfilePage> createState() => _UserProfilePageState();
}

class _UserProfilePageState extends State<UserProfilePage> {
  late AppProfile _profile;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _profile = widget.initialProfile;
    _refresh();
    _logProfileView();
  }

  Future<void> _logProfileView() async {
    try {
      await widget.session.api.logActivity(
        eventType: 'profile_viewed',
        targetUserId: widget.initialProfile.userId,
        discoveryMode: widget.initialProfile.discoveryMode ?? 'direct',
        metadata: const {'surface': 'profile_page'},
      );
    } catch (_) {
      // Ignore analytics failures.
    }
  }

  Future<void> _refresh() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _profile = await widget.session.api.fetchUser(widget.initialProfile.userId);
    } on ApiException catch (error) {
      _error = error.message;
    } finally {
      if (mounted) {
        setState(() {
          _loading = false;
        });
      }
    }
  }

  Future<void> _sendRequest() async {
    try {
      await widget.session.api.sendFriendRequest(targetUserId: _profile.userId);
      widget.onSocialChange();
      await _refresh();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(error.message)),
      );
    }
  }

  Future<void> _tapPhone() async {
    try {
      await widget.session.api.logActivity(
        eventType: 'call_tapped',
        targetUserId: _profile.userId,
        discoveryMode: _profile.discoveryMode ?? 'direct',
        metadata: const {'surface': 'profile_page'},
      );
    } catch (_) {
      // Ignore analytics failures.
    }
    await launchExternalUri('tel:${_profile.phoneNumber}');
  }

  Future<void> _tapEmail() async {
    try {
      await widget.session.api.logActivity(
        eventType: 'email_tapped',
        targetUserId: _profile.userId,
        discoveryMode: _profile.discoveryMode ?? 'direct',
        metadata: const {'surface': 'profile_page'},
      );
    } catch (_) {
      // Ignore analytics failures.
    }
    await launchExternalUri('mailto:${_profile.email}');
  }

  @override
  Widget build(BuildContext context) {
    return AppBackdrop(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        appBar: AppBar(backgroundColor: Colors.transparent),
        body: RefreshIndicator(
          onRefresh: _refresh,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
            children: [
              if (_loading)
                const Center(
                  child: Padding(
                    padding: EdgeInsets.only(top: 48),
                    child: CircularProgressIndicator(),
                  ),
                )
              else if (_error != null)
                ErrorCard(message: _error!)
              else ...[
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(22),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          _profile.displayName,
                          style: Theme.of(context)
                              .textTheme
                              .headlineSmall
                              ?.copyWith(fontWeight: FontWeight.w800),
                        ),
                        const SizedBox(height: 6),
                        Text('@${_profile.username}'),
                        const SizedBox(height: 16),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            StatusChip(label: _profile.friendStatus.replaceAll('_', ' ')),
                            if (_profile.graphScore != null)
                              ScoreChip(
                                label:
                                    'Graph ${(100 * _profile.graphScore!).round()}%',
                              ),
                            if (_profile.matchPercent != null)
                              ScoreChip(
                                label: 'Compatibility ${_profile.matchPercent}%',
                              ),
                            if (_profile.matchedFromContacts)
                              const StatusChip(label: 'In contacts'),
                          ],
                        ),
                        const SizedBox(height: 18),
                        if (_profile.description.isNotEmpty)
                          Text(
                            _profile.description,
                            style: Theme.of(context).textTheme.bodyLarge,
                          ),
                        if (_profile.matchSummary != null) ...[
                          const SizedBox(height: 18),
                          Text(
                            _profile.matchSummary!.friendshipSummary,
                            style: Theme.of(context)
                                .textTheme
                                .bodyMedium
                                ?.copyWith(fontWeight: FontWeight.w600),
                          ),
                        ],
                        if (_profile.topTraits.isNotEmpty) ...[
                          const SizedBox(height: 16),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: _profile.topTraits
                                .map((trait) => Chip(label: Text(trait.label)))
                                .toList(),
                          ),
                        ],
                        const SizedBox(height: 20),
                        Wrap(
                          spacing: 10,
                          runSpacing: 10,
                          children: [
                            if (_profile.friendStatus == 'none')
                              FilledButton(
                                onPressed: _sendRequest,
                                child: const Text('Send request'),
                              ),
                            if (_profile.friendStatus == 'incoming_pending')
                              OutlinedButton(
                                onPressed: () {
                                  Navigator.of(context).pop();
                                  widget.onOpenRequests();
                                },
                                child: const Text('Open requests'),
                              ),
                            if ((_profile.phoneNumber ?? '').isNotEmpty)
                              FilledButton.tonalIcon(
                                onPressed: _tapPhone,
                                icon: const Icon(Icons.call_rounded),
                                label: Text(_profile.phoneNumber!),
                              ),
                            if ((_profile.email ?? '').isNotEmpty)
                              FilledButton.tonalIcon(
                                onPressed: _tapEmail,
                                icon: const Icon(Icons.email_rounded),
                                label: Text(_profile.email!),
                              ),
                          ],
                        ),
                        if ((_profile.phoneNumber ?? '').isEmpty &&
                            (_profile.email ?? '').isEmpty) ...[
                          const SizedBox(height: 16),
                          const InfoBanner(
                            message:
                                'Phone and email stay hidden until the friendship is accepted and sharing is enabled.',
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class PersonCard extends StatelessWidget {
  const PersonCard({
    super.key,
    required this.profile,
    required this.onTap,
    required this.action,
  });

  final AppProfile profile;
  final VoidCallback onTap;
  final Widget action;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(24),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          profile.displayName,
                          style: Theme.of(context)
                              .textTheme
                              .titleLarge
                              ?.copyWith(fontWeight: FontWeight.w700),
                        ),
                        const SizedBox(height: 4),
                        Text('@${profile.username}'),
                      ],
                    ),
                  ),
                  if (profile.matchPercent != null)
                    ScoreChip(
                      label: '${profile.matchPercent}% compatibility',
                    ),
                ],
              ),
              const SizedBox(height: 12),
              Text(
                profile.description.isEmpty
                    ? 'No description yet.'
                    : profile.description,
                maxLines: 4,
                overflow: TextOverflow.ellipsis,
              ),
              const SizedBox(height: 14),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (profile.graphScore != null)
                    ScoreChip(
                      label: 'Graph ${(100 * profile.graphScore!).round()}%',
                    ),
                  if (profile.scoreComponents['query_fit_score'] != null &&
                      (profile.scoreComponents['query_fit_score'] ?? 0) > 0)
                    ScoreChip(
                      label:
                          'Query ${((profile.scoreComponents['query_fit_score'] ?? 0) * 100).round()}%',
                    ),
                  if (profile.matchedFromContacts)
                    const StatusChip(label: 'In contacts'),
                  ...profile.topTraits
                      .take(2)
                      .map((trait) => Chip(label: Text(trait.label))),
                ],
              ),
              if (profile.matchSummary != null) ...[
                const SizedBox(height: 12),
                Text(
                  profile.matchSummary!.friendshipSummary,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                ),
              ],
              const SizedBox(height: 16),
              Align(alignment: Alignment.centerRight, child: action),
            ],
          ),
        ),
      ),
    );
  }
}

class RequestCard extends StatelessWidget {
  const RequestCard({
    super.key,
    required this.row,
    required this.onTap,
    required this.trailing,
  });

  final FriendRequestRow row;
  final VoidCallback onTap;
  final Widget trailing;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(24),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                row.counterparty.displayName,
                style: Theme.of(context)
                    .textTheme
                    .titleLarge
                    ?.copyWith(fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 6),
              Text('@${row.counterparty.username}'),
              if (row.message.isNotEmpty) ...[
                const SizedBox(height: 10),
                Text(row.message),
              ],
              const SizedBox(height: 14),
              Align(alignment: Alignment.centerRight, child: trailing),
            ],
          ),
        ),
      ),
    );
  }
}

class AppBackdrop extends StatelessWidget {
  const AppBackdrop({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            Color(0xFFF8F5EC),
            Color(0xFFDDEFEA),
            Color(0xFFF5E6D8),
          ],
        ),
      ),
      child: child,
    );
  }
}

class ErrorBanner extends StatelessWidget {
  const ErrorBanner({super.key, required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFFFEE2E2),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Text(message),
    );
  }
}

class InfoBanner extends StatelessWidget {
  const InfoBanner({super.key, required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFFDBEAFE),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Text(message),
    );
  }
}

class ErrorCard extends StatelessWidget {
  const ErrorCard({super.key, required this.message});

  final String message;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: ErrorBanner(message: message),
      ),
    );
  }
}

class EmptyCard extends StatelessWidget {
  const EmptyCard({super.key, required this.title, required this.message});

  final String title;
  final String message;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(22),
        child: Column(
          children: [
            Text(
              title,
              style: Theme.of(context)
                  .textTheme
                  .titleLarge
                  ?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 10),
            Text(message, textAlign: TextAlign.center),
          ],
        ),
      ),
    );
  }
}

class SectionHeader extends StatelessWidget {
  const SectionHeader({super.key, required this.title});

  final String title;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Text(
        title,
        style: Theme.of(context)
            .textTheme
            .titleMedium
            ?.copyWith(fontWeight: FontWeight.w700),
      ),
    );
  }
}

class StatusChip extends StatelessWidget {
  const StatusChip({super.key, required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: const Color(0xFFCCFBF1),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: Theme.of(context)
            .textTheme
            .labelLarge
            ?.copyWith(fontWeight: FontWeight.w700),
      ),
    );
  }
}

class ScoreChip extends StatelessWidget {
  const ScoreChip({super.key, required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: const Color(0xFFFFEDD5),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: Theme.of(context)
            .textTheme
            .labelLarge
            ?.copyWith(fontWeight: FontWeight.w700),
      ),
    );
  }
}

Future<void> showBackendSettingsSheet(
  BuildContext context, {
  required SessionController session,
}) async {
  final controller = TextEditingController(text: session.baseUrl);
  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    builder: (context) {
      return Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          20,
          20,
          MediaQuery.of(context).viewInsets.bottom + 20,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Backend URL',
              style: Theme.of(context)
                  .textTheme
                  .titleLarge
                  ?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 10),
            const Text(
              'Use http://10.0.2.2:8000 for the Android emulator, or your computer LAN IP when testing from a real phone.',
            ),
            const SizedBox(height: 14),
            TextField(
              controller: controller,
              decoration: const InputDecoration(
                labelText: 'http://192.168.x.x:8000',
              ),
            ),
            const SizedBox(height: 14),
            FilledButton(
              onPressed: () async {
                final navigator = Navigator.of(context);
                await session.setBaseUrl(controller.text);
                if (navigator.mounted) {
                  navigator.pop();
                }
              },
              child: const Text('Save backend URL'),
            ),
          ],
        ),
      );
    },
  );
  controller.dispose();
}

class ContactImportResult {
  const ContactImportResult({
    required this.message,
    this.permanentlyDenied = false,
  });

  final String message;
  final bool permanentlyDenied;
}

Future<ContactImportResult> importContactsFromDevice(
  SessionController session,
) async {
  final contactsPermission = await FlutterContacts.permissions.request(
    PermissionType.read,
  );
  final granted = contactsPermission == PermissionStatus.granted ||
      contactsPermission == PermissionStatus.limited;

  if (!granted) {
    await session.updateMe(<String, dynamic>{'contacts_permission_granted': false});
    return ContactImportResult(
      message: 'Contacts were skipped. Matching still works without them.',
      permanentlyDenied:
          contactsPermission == PermissionStatus.permanentlyDenied ||
          contactsPermission == PermissionStatus.restricted,
    );
  }

  await session.updateMe(<String, dynamic>{'contacts_permission_granted': true});
  final contacts = await FlutterContacts.getAll(
    properties: {
      ContactProperty.name,
      ContactProperty.phone,
      ContactProperty.email,
    },
    limit: 250,
  );
  final payload = <Map<String, String>>[];

  for (final contact in contacts) {
    final phone = contact.phones.isNotEmpty ? contact.phones.first.number : '';
    final email = contact.emails.isNotEmpty ? contact.emails.first.address : '';
    final fullName = (contact.displayName ?? '').trim();
    if (fullName.isEmpty && phone.isEmpty && email.isEmpty) {
      continue;
    }
    payload.add(
      <String, String>{
        'full_name': fullName,
        'phone_number': phone,
        'email': email,
      },
    );
    if (payload.length >= 250) {
      break;
    }
  }

  if (payload.isNotEmpty) {
    await session.api.importContacts(payload);
  }
  await session.refreshMe();
  return ContactImportResult(
    message: payload.isEmpty
        ? 'Contacts permission granted. No importable contacts were found.'
        : 'Imported ${payload.length} contacts for matching.',
  );
}

Future<void> launchExternalUri(String raw) async {
  final uri = Uri.parse(raw);
  await launchUrl(uri, mode: LaunchMode.externalApplication);
}
