import 'package:flutter/material.dart';

const String kAppFontFamily = 'LydianBT';

ThemeData buildHelloAgainTheme({
  required Color scaffoldBackgroundColor,
  required Color seedColor,
  required Color surfaceColor,
}) {
  final base = ThemeData(
    useMaterial3: true,
    fontFamily: kAppFontFamily,
    scaffoldBackgroundColor: scaffoldBackgroundColor,
    colorScheme: ColorScheme.fromSeed(
      seedColor: seedColor,
      brightness: Brightness.light,
      surface: surfaceColor,
    ),
  );

  final textTheme = base.textTheme.apply(
    fontFamily: kAppFontFamily,
    displayColor: base.colorScheme.onSurface,
    bodyColor: base.colorScheme.onSurface,
  );

  final primaryTextTheme = base.primaryTextTheme.apply(
    fontFamily: kAppFontFamily,
  );

  return base.copyWith(
    textTheme: textTheme,
    primaryTextTheme: primaryTextTheme,
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
