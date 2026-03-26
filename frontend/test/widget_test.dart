import 'package:flutter_test/flutter_test.dart';
import 'package:frontend/main.dart';

void main() {
  testWidgets('App smoke test — renders without crashing',
      (WidgetTester tester) async {
    await tester.pumpWidget(const HelloAgainApp());
    // PermissionScreen should be on-screen
    expect(find.text('HelloAgain'), findsWidgets);
  });
}
