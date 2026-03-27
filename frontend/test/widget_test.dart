import 'package:flutter_test/flutter_test.dart';
import 'package:frontend/main.dart';

void main() {
  testWidgets('App smoke test renders the voice lab shell', (
    WidgetTester tester,
  ) async {
    await tester.pumpWidget(const HelloAgainApp());
    await tester.pump();
    expect(find.text('Voice Lab'), findsWidgets);
  });
}
