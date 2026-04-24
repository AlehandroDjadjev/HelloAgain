import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:frontend/whitespace_app.dart';

void main() {
  Future<void> pumpDialog(
    WidgetTester tester,
    Map<String, dynamic> viewer,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AgentResultDialog(viewer: viewer),
        ),
      ),
    );
    await tester.pumpAndSettle();
  }

  testWidgets('summary viewer does not render raw json payload', (tester) async {
    await pumpDialog(tester, {
      'widget_type': 'summary_only',
      'title': 'Уточнение',
      'summary': 'Нужно е още малко пояснение.',
      'payload': {
        'object': {'name': 'clarify_input'},
        'linked_results': [],
      },
    });

    expect(find.text('Нужно е още малко пояснение.'), findsWidgets);
    expect(find.textContaining('linked_results'), findsNothing);
    expect(find.textContaining('clarify_input'), findsNothing);
  });

  testWidgets('weather viewer shows bulgarian weather card', (tester) async {
    await pumpDialog(tester, {
      'widget_type': 'weather_snapshot',
      'title': 'Времето сега',
      'summary': 'Ясно, 24°C',
      'weather': {
        'label': 'Ясно',
        'summary': 'Ясно, 24°C',
        'advice': 'Подходящо е за кратка разходка.',
        'temperature_c': 24,
        'apparent_temperature_c': 25,
        'wind_speed': 8,
        'wind_unit': 'km/h',
        'icon_key': 'sun',
      },
    });

    expect(find.text('Времето сега'), findsOneWidget);
    expect(find.text('Ясно'), findsOneWidget);
    expect(find.text('Подходящо е за кратка разходка.'), findsOneWidget);
    expect(find.textContaining('weather_code'), findsNothing);
  });

  testWidgets('outing viewer shows person and place without payload dump', (
    tester,
  ) async {
    await pumpDialog(tester, {
      'widget_type': 'outing_suggestion',
      'title': 'Навън с Мария',
      'user': {
        'display_name': 'Мария',
        'description': 'Обича спокойни разговори и разходки.',
      },
      'outing': {
        'place_name': 'Южен парк',
        'recommended_when_bg': 'днес в 17:30',
        'weather': 'Слънчево',
        'score': 0.92,
      },
      'payload': {
        'debug_only': true,
      },
    });

    expect(find.textContaining('Мария'), findsWidgets);
    expect(find.text('Южен парк'), findsOneWidget);
    expect(find.textContaining('debug_only'), findsNothing);
  });
}
