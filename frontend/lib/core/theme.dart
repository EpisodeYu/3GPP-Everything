import 'package:flutter/material.dart';

/// 黑白主调 + 冷调蓝 accent。
/// 设计语言来自 docs/03-development/05-frontend.md §10。
///
/// 质感优化（2026-07）：
/// - 全局字体：Inter（拉丁/数字，可变字体覆盖全字重）+ NotoSansSC（CJK fallback），
///   彻底摆脱 Web 端默认 Roboto「细而糊」与中文走 CDN 兜底失败。
/// - 显式 [TextTheme]：统一字重 / 行高 / 字距 / 次要文字对比度。
/// - 统一组件主题（divider / card / input / dialog / popup / snackbar / chip …）
///   与轻量层次（[softShadow]），提升高级感而不喧宾夺主。
class AppTheme {
  AppTheme._();

  static const Color _seed = Color(0xFF4F6D7A);

  static const double radiusCard = 16;
  static const double radiusChip = 999;
  static const double radiusButton = 12;
  static const double radiusField = 12;

  /// 聊天气泡 / 浮层卡片的统一圆角。
  static const double radiusBubble = 16;

  static const String _fontFamily = 'Inter';
  static const List<String> _fontFallback = <String>['NotoSansSC'];

  static ThemeData light() => _build(Brightness.light);
  static ThemeData dark() => _build(Brightness.dark);

  /// 极轻的投影：给卡片 / 气泡 / 浮层一点层次感。
  /// light 下用两层近乎透明的黑影模拟纸面浮起；dark 下几乎不需要投影。
  static List<BoxShadow> softShadow(Brightness brightness) {
    if (brightness == Brightness.dark) {
      return const [
        BoxShadow(
          color: Color(0x33000000),
          blurRadius: 10,
          offset: Offset(0, 2),
        ),
      ];
    }
    return const [
      BoxShadow(color: Color(0x0F0B1220), blurRadius: 14, offset: Offset(0, 4)),
      BoxShadow(color: Color(0x0A0B1220), blurRadius: 2, offset: Offset(0, 1)),
    ];
  }

  static ThemeData _build(Brightness brightness) {
    final isDark = brightness == Brightness.dark;
    final scheme =
        ColorScheme.fromSeed(seedColor: _seed, brightness: brightness).copyWith(
          surface: isDark ? const Color(0xFF0E0E0E) : Colors.white,
          onSurface: isDark ? const Color(0xFFEDEDED) : const Color(0xFF1A1A1A),
          // 次要文字：比 M3 默认更实一点，解决「灰得看不清」。
          onSurfaceVariant: isDark
              ? const Color(0xFFB4B9C1)
              : const Color(0xFF565B63),
          // 分层灰阶：从最底到最高，给侧栏 / 卡片 / 气泡 / 选中态提供细腻梯度。
          surfaceContainerLowest: isDark
              ? const Color(0xFF080808)
              : Colors.white,
          surfaceContainerLow: isDark
              ? const Color(0xFF151515)
              : const Color(0xFFFAFAFB),
          surfaceContainer: isDark
              ? const Color(0xFF1C1C1C)
              : const Color(0xFFF4F5F6),
          surfaceContainerHigh: isDark
              ? const Color(0xFF232323)
              : const Color(0xFFEDEEF0),
          surfaceContainerHighest: isDark
              ? const Color(0xFF2A2A2A)
              : const Color(0xFFE7E9EC),
          // 边框：可见但不生硬；outlineVariant 更淡，用于分隔线 / 卡片发丝边。
          outline: isDark ? const Color(0xFF3A3A3A) : const Color(0xFFD7D9DE),
          outlineVariant: isDark
              ? const Color(0xFF262626)
              : const Color(0xFFEBECEF),
        );

    final textTheme = _textTheme(scheme, isDark);

    return ThemeData(
      colorScheme: scheme,
      useMaterial3: true,
      brightness: brightness,
      scaffoldBackgroundColor: scheme.surface,
      fontFamily: _fontFamily,
      fontFamilyFallback: _fontFallback,
      textTheme: textTheme,
      dividerTheme: DividerThemeData(
        color: scheme.outlineVariant,
        thickness: 1,
        space: 1,
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        color: scheme.surfaceContainerLow,
        surfaceTintColor: Colors.transparent,
        shadowColor: Colors.black.withValues(alpha: isDark ? 0.4 : 0.06),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radiusCard),
          side: BorderSide(color: scheme.outlineVariant),
        ),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: scheme.surfaceContainer,
        side: BorderSide(color: scheme.outlineVariant),
        labelStyle: textTheme.labelMedium,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(radiusChip),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          elevation: 0,
          textStyle: textTheme.labelLarge?.copyWith(
            fontWeight: FontWeight.w600,
          ),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(radiusButton),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: scheme.onSurface,
          textStyle: textTheme.labelLarge?.copyWith(
            fontWeight: FontWeight.w600,
          ),
          side: BorderSide(color: scheme.outline),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(radiusButton),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 13),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          textStyle: textTheme.labelLarge?.copyWith(
            fontWeight: FontWeight.w600,
          ),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(radiusButton),
          ),
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(foregroundColor: scheme.onSurfaceVariant),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: isDark
            ? scheme.surfaceContainer
            : scheme.surfaceContainerLow,
        hintStyle: textTheme.bodyMedium?.copyWith(
          color: scheme.onSurfaceVariant,
        ),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 16,
          vertical: 14,
        ),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radiusField),
          borderSide: BorderSide(color: scheme.outline),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radiusField),
          borderSide: BorderSide(color: scheme.outline),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(radiusField),
          borderSide: BorderSide(color: scheme.primary, width: 1.6),
        ),
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: scheme.surface,
        foregroundColor: scheme.onSurface,
        elevation: 0,
        scrolledUnderElevation: 0.5,
        surfaceTintColor: Colors.transparent,
        shadowColor: Colors.black.withValues(alpha: 0.06),
        titleTextStyle: textTheme.titleLarge,
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: scheme.surface,
        surfaceTintColor: Colors.transparent,
        elevation: 6,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        titleTextStyle: textTheme.titleLarge,
        contentTextStyle: textTheme.bodyMedium,
      ),
      popupMenuTheme: PopupMenuThemeData(
        color: scheme.surface,
        surfaceTintColor: Colors.transparent,
        elevation: 3,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: scheme.outlineVariant),
        ),
        textStyle: textTheme.bodyMedium,
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        contentTextStyle: textTheme.bodyMedium?.copyWith(
          color: scheme.onInverseSurface,
        ),
      ),
      listTileTheme: ListTileThemeData(
        iconColor: scheme.onSurfaceVariant,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
      tooltipTheme: TooltipThemeData(
        decoration: BoxDecoration(
          color: scheme.inverseSurface,
          borderRadius: BorderRadius.circular(8),
        ),
        textStyle: textTheme.labelSmall?.copyWith(
          color: scheme.onInverseSurface,
        ),
      ),
      scrollbarTheme: ScrollbarThemeData(
        thumbColor: WidgetStatePropertyAll(
          scheme.onSurface.withValues(alpha: 0.18),
        ),
        radius: const Radius.circular(999),
        thickness: const WidgetStatePropertyAll(6),
      ),
    );
  }

  /// 基于 M3 baseline 尺寸，仅调字体 / 字重 / 行高 / 字距 / 次要色，
  /// 不改字号，保持既有布局稳定。
  static TextTheme _textTheme(ColorScheme scheme, bool isDark) {
    final base =
        (isDark
                ? Typography.material2021().white
                : Typography.material2021().black)
            .apply(
              fontFamily: _fontFamily,
              fontFamilyFallback: _fontFallback,
              bodyColor: scheme.onSurface,
              displayColor: scheme.onSurface,
            );

    TextStyle? heading(TextStyle? s, double height) => s?.copyWith(
      fontWeight: FontWeight.w600,
      height: height,
      letterSpacing: -0.2,
    );

    return base.copyWith(
      displayLarge: heading(base.displayLarge, 1.14),
      displayMedium: heading(base.displayMedium, 1.15),
      displaySmall: heading(base.displaySmall, 1.18),
      headlineLarge: heading(base.headlineLarge, 1.2),
      headlineMedium: heading(base.headlineMedium, 1.22),
      headlineSmall: heading(base.headlineSmall, 1.25),
      titleLarge: base.titleLarge?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.3,
        letterSpacing: -0.1,
      ),
      titleMedium: base.titleMedium?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.3,
      ),
      titleSmall: base.titleSmall?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.3,
      ),
      bodyLarge: base.bodyLarge?.copyWith(height: 1.55, letterSpacing: 0.1),
      bodyMedium: base.bodyMedium?.copyWith(height: 1.55, letterSpacing: 0.1),
      bodySmall: base.bodySmall?.copyWith(
        height: 1.45,
        letterSpacing: 0.1,
        color: scheme.onSurfaceVariant,
      ),
      labelLarge: base.labelLarge?.copyWith(
        fontWeight: FontWeight.w600,
        letterSpacing: 0.1,
      ),
      labelMedium: base.labelMedium?.copyWith(
        fontWeight: FontWeight.w500,
        letterSpacing: 0.2,
      ),
      labelSmall: base.labelSmall?.copyWith(
        fontWeight: FontWeight.w500,
        letterSpacing: 0.3,
        color: scheme.onSurfaceVariant,
      ),
    );
  }
}
