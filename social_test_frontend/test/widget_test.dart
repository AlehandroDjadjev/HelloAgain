import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:social_test_frontend/api.dart';
import 'package:social_test_frontend/main.dart';

void main() {
  testWidgets('App boots to auth screen when logged out', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    final preferences = await SharedPreferences.getInstance();
    final session = SessionController(preferences: preferences);
    await session.bootstrap();

    await tester.pumpWidget(SocialTestApp(session: session));
    await tester.pumpAndSettle();

    expect(find.text('HelloAgain Social Test'), findsWidgets);
    expect(find.text('Login'), findsOneWidget);
  });
}
