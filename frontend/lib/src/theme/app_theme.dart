import 'package:flutter/material.dart';

class HelloAgainPalette {
  static const bloodRed = Color(0xFF8C1C13);
  static const blushedBrick = Color(0xFFBF4342);
  static const almondCream = Color(0xFFE7D7C1);
  static const whiteSmoke = Color(0xFFF7F4F3);
  static const dustGrey = Color(0xFFE6DDDB);
  static const ink = Color(0xFF2E1B1A);

  const HelloAgainPalette._();
}

ThemeData buildHelloAgainTheme({
  required Color scaffoldBackgroundColor,
  required Color seedColor,
  required Color surfaceColor,
}) {
  final baseScheme = ColorScheme.fromSeed(
    seedColor: seedColor,
    brightness: Brightness.light,
    surface: surfaceColor,
  );
  final colorScheme = baseScheme.copyWith(
    primary: HelloAgainPalette.blushedBrick,
    onPrimary: HelloAgainPalette.whiteSmoke,
    primaryContainer: HelloAgainPalette.almondCream,
    onPrimaryContainer: HelloAgainPalette.ink,
    secondary: HelloAgainPalette.bloodRed,
    onSecondary: HelloAgainPalette.whiteSmoke,
    secondaryContainer: HelloAgainPalette.dustGrey,
    onSecondaryContainer: HelloAgainPalette.ink,
    tertiary: HelloAgainPalette.almondCream,
    onTertiary: HelloAgainPalette.ink,
    surface: surfaceColor,
    onSurface: HelloAgainPalette.ink,
    surfaceContainerHighest: HelloAgainPalette.dustGrey,
    outline: HelloAgainPalette.dustGrey,
    outlineVariant: HelloAgainPalette.dustGrey,
    error: HelloAgainPalette.bloodRed,
    onError: HelloAgainPalette.whiteSmoke,
    errorContainer: HelloAgainPalette.blushedBrick.withValues(alpha: 0.14),
    onErrorContainer: HelloAgainPalette.bloodRed,
  );

  final base = ThemeData(
    useMaterial3: true,
    scaffoldBackgroundColor: scaffoldBackgroundColor,
    colorScheme: colorScheme,
  );
  final textTheme = base.textTheme
      .apply(
        displayColor: colorScheme.onSurface,
        bodyColor: colorScheme.onSurface,
      )
      .copyWith(
        displayLarge: base.textTheme.displayLarge?.copyWith(
          letterSpacing: -0.8,
        ),
        displayMedium: base.textTheme.displayMedium?.copyWith(
          letterSpacing: -0.8,
        ),
        headlineLarge: base.textTheme.headlineLarge?.copyWith(
          letterSpacing: -0.6,
        ),
        headlineMedium: base.textTheme.headlineMedium?.copyWith(
          letterSpacing: -0.5,
        ),
        titleLarge: base.textTheme.titleLarge?.copyWith(letterSpacing: -0.3),
        bodyLarge: base.textTheme.bodyLarge?.copyWith(height: 1.45),
        bodyMedium: base.textTheme.bodyMedium?.copyWith(height: 1.45),
      );

  return base.copyWith(
    textTheme: textTheme,
    appBarTheme: base.appBarTheme.copyWith(
      backgroundColor: Colors.transparent,
      foregroundColor: colorScheme.onSurface,
      titleTextStyle: textTheme.titleLarge?.copyWith(
        fontWeight: FontWeight.w700,
        color: colorScheme.onSurface,
      ),
      toolbarTextStyle: textTheme.bodyMedium,
      elevation: 0,
    ),
    snackBarTheme: base.snackBarTheme.copyWith(
      backgroundColor: HelloAgainPalette.bloodRed,
      contentTextStyle: textTheme.bodyMedium?.copyWith(
        color: HelloAgainPalette.whiteSmoke,
      ),
    ),
    chipTheme: base.chipTheme.copyWith(
      backgroundColor: HelloAgainPalette.dustGrey,
      selectedColor: HelloAgainPalette.almondCream,
      side: BorderSide.none,
      labelStyle: textTheme.labelLarge?.copyWith(
        color: HelloAgainPalette.ink,
        fontWeight: FontWeight.w600,
      ),
      secondaryLabelStyle: textTheme.labelLarge?.copyWith(
        color: HelloAgainPalette.ink,
        fontWeight: FontWeight.w600,
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: HelloAgainPalette.bloodRed,
        foregroundColor: HelloAgainPalette.whiteSmoke,
        disabledBackgroundColor: HelloAgainPalette.dustGrey,
        disabledForegroundColor: HelloAgainPalette.ink.withValues(alpha: 0.52),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(22)),
        textStyle: textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: HelloAgainPalette.bloodRed,
        side: const BorderSide(color: HelloAgainPalette.blushedBrick),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(22)),
        textStyle: textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: HelloAgainPalette.whiteSmoke,
      contentPadding: const EdgeInsets.all(18),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(22),
        borderSide: const BorderSide(color: HelloAgainPalette.dustGrey),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(22),
        borderSide: const BorderSide(color: HelloAgainPalette.dustGrey),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(22),
        borderSide: const BorderSide(
          color: HelloAgainPalette.blushedBrick,
          width: 1.5,
        ),
      ),
    ),
  );
}
