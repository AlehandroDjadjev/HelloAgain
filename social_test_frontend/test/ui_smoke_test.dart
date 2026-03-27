import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:social_test_frontend/main.dart';

void main() {
  testWidgets('status and score chips render', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(
          body: Column(
            children: [
              StatusChip(label: 'Friends'),
              ScoreChip(label: '92% fit'),
            ],
          ),
        ),
      ),
    );

    expect(find.text('Friends'), findsOneWidget);
    expect(find.text('92% fit'), findsOneWidget);
  });
}
