import 'package:flutter/material.dart';

ThemeData buildHelloAgainTheme({
  required Color scaffoldBackgroundColor,
  required Color seedColor,
  required Color surfaceColor,
}) {
  final base = ThemeData(
    useMaterial3: true,
    scaffoldBackgroundColor: scaffoldBackgroundColor,
    colorScheme: ColorScheme.fromSeed(
      seedColor: seedColor,
      brightness: Brightness.light,
      surface: surfaceColor,
    ),
  );
  final textTheme = base.textTheme.apply(
    displayColor: base.colorScheme.onSurface,
    bodyColor: base.colorScheme.onSurface,
  );

  return base.copyWith(
    textTheme: textTheme,
    appBarTheme: base.appBarTheme.copyWith(
      titleTextStyle: textTheme.titleLarge,
      toolbarTextStyle: textTheme.bodyMedium,
    ),
    snackBarTheme: base.snackBarTheme.copyWith(
      contentTextStyle: textTheme.bodyMedium,
    ),
    chipTheme: base.chipTheme.copyWith(
      labelStyle: textTheme.labelLarge,
      secondaryLabelStyle: textTheme.labelLarge,
    ),
  );
}
