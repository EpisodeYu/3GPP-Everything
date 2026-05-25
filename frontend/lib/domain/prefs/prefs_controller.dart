import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// 用户偏好（M5.6）：主题模式 + 语言。
///
/// 与 token_store（`flutter_secure_storage`）分开存：
/// - secure_storage 装鉴权 token 等敏感信息
/// - shared_preferences 装非敏感 UI 偏好（theme / locale）
///
/// `locale == null` → 跟随系统；`themeMode == ThemeMode.system` → 跟随系统。
///
/// 锚：`docs/03-development/05-frontend.md §9 / §10`。
class AppPrefs {
  const AppPrefs({required this.themeMode, required this.locale});

  final ThemeMode themeMode;

  /// 用户显式选择的 locale；null = 跟随系统（MaterialApp.localeResolutionCallback 兜底）。
  final Locale? locale;

  static const AppPrefs defaults = AppPrefs(
    themeMode: ThemeMode.system,
    locale: null,
  );

  AppPrefs copyWith({ThemeMode? themeMode, Object? locale = _sentinel}) {
    return AppPrefs(
      themeMode: themeMode ?? this.themeMode,
      locale: identical(locale, _sentinel) ? this.locale : locale as Locale?,
    );
  }
}

const _sentinel = Object();

/// shared_preferences key namespace。
const _kThemeKey = 'tgpp.prefs.themeMode';
const _kLocaleKey = 'tgpp.prefs.locale';

/// `null` 表示宿主未在 main() 注入 prefs（绝大多数 widget test 走这条路径）。
/// 此时 [PrefsController] 退化到内存中维护偏好，不写盘 → 测试不需要额外 override
/// 这个 provider 也能跑过。
final sharedPreferencesProvider = Provider<SharedPreferences?>((ref) => null);

final prefsControllerProvider =
    NotifierProvider<PrefsController, AppPrefs>(PrefsController.new);

class PrefsController extends Notifier<AppPrefs> {
  @override
  AppPrefs build() {
    final prefs = ref.read(sharedPreferencesProvider);
    if (prefs == null) return AppPrefs.defaults;
    return AppPrefs(
      themeMode: _decodeTheme(prefs.getString(_kThemeKey)),
      locale: _decodeLocale(prefs.getString(_kLocaleKey)),
    );
  }

  Future<void> setThemeMode(ThemeMode mode) async {
    state = state.copyWith(themeMode: mode);
    final prefs = ref.read(sharedPreferencesProvider);
    if (prefs == null) return;
    await prefs.setString(_kThemeKey, _encodeTheme(mode));
  }

  /// `null` = 跟随系统。
  Future<void> setLocale(Locale? locale) async {
    state = state.copyWith(locale: locale);
    final prefs = ref.read(sharedPreferencesProvider);
    if (prefs == null) return;
    if (locale == null) {
      await prefs.remove(_kLocaleKey);
    } else {
      await prefs.setString(_kLocaleKey, locale.toLanguageTag());
    }
  }

  static String _encodeTheme(ThemeMode mode) => switch (mode) {
        ThemeMode.light => 'light',
        ThemeMode.dark => 'dark',
        ThemeMode.system => 'system',
      };

  static ThemeMode _decodeTheme(String? raw) => switch (raw) {
        'light' => ThemeMode.light,
        'dark' => ThemeMode.dark,
        _ => ThemeMode.system,
      };

  static Locale? _decodeLocale(String? raw) {
    if (raw == null || raw.isEmpty) return null;
    final parts = raw.split('-');
    if (parts.length == 1) return Locale(parts[0]);
    return Locale(parts[0], parts.sublist(1).join('_'));
  }
}
