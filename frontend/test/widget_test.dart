import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:frontend/main.dart';

void main() {
  testWidgets('app boots then shows onboarding gate with actionable control', (
    WidgetTester tester,
  ) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const HelloAgainApp());

    expect(find.text('Hello Again'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(find.text('Step 1 of 2'), findsOneWidget);
    expect(find.byType(FilledButton), findsOneWidget);
    final primaryAction = tester.widget<FilledButton>(find.byType(FilledButton));
    expect(primaryAction.onPressed, isNotNull);
  });
}
