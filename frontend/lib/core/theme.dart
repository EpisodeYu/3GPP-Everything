import 'package:flutter/material.dart';

/// 黑白主调 + 冷调蓝 accent。
/// 设计语言来自 docs/03-development/05-frontend.md §10。
class AppTheme {
  AppTheme._();

  static const Color _seed = Color(0xFF4F6D7A);

  static const double _radiusCard = 16;
  static const double _radiusChip = 999;
  static const double _radiusButton = 12;

  static ThemeData light() => _build(Brightness.light);
  static ThemeData dark() => _build(Brightness.dark);

  static ThemeData _build(Brightness brightness) {
    final isDark = brightness == Brightness.dark;
    final scheme = ColorScheme.fromSeed(
      seedColor: _seed,
      brightness: brightness,
    ).copyWith(
      surface: isDark ? const Color(0xFF0E0E0E) : Colors.white,
      onSurface: isDark ? const Color(0xFFEDEDED) : const Color(0xFF1A1A1A),
      surfaceContainer:
          isDark ? const Color(0xFF1C1C1C) : const Color(0xFFF5F5F5),
      outline: isDark ? const Color(0xFF2A2A2A) : const Color(0xFFE5E5E5),
    );

    return ThemeData(
      colorScheme: scheme,
      useMaterial3: true,
      scaffoldBackgroundColor: scheme.surface,
      cardTheme: CardThemeData(
        elevation: 0,
        color: scheme.surfaceContainer,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(_radiusCard),
          side: BorderSide(color: scheme.outline),
        ),
      ),
      chipTheme: ChipThemeData(
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(_radiusChip),
        ),
        side: BorderSide(color: scheme.outline),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(_radiusButton),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(_radiusButton),
          ),
          side: BorderSide(color: scheme.outline),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(_radiusButton),
          borderSide: BorderSide(color: scheme.outline),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(_radiusButton),
          borderSide: BorderSide(color: scheme.outline),
        ),
      ),
    );
  }
}
