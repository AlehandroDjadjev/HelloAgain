import 'package:flutter_test/flutter_test.dart';

import 'package:whitespace/main.dart';

void main() {
  testWidgets('agent board app renders', (WidgetTester tester) async {
    await tester.pumpWidget(const AgentBoardApp());

    expect(find.text('Agent Space'), findsNothing);
    expect(find.textContaining('Semi Agent'), findsOneWidget);
  });
}
