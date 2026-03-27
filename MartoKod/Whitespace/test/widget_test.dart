import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:whitespace/main.dart';

void main() {
  testWidgets('hello again intro renders', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const AgentBoardApp());
    await tester.pump();

    expect(find.text('Hello Again'), findsOneWidget);
  });
}
