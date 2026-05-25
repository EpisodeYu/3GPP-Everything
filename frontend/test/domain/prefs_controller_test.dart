import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tgpp/domain/prefs/prefs_controller.dart';

ProviderContainer _makeContainer(SharedPreferences? prefs) {
  return ProviderContainer(
    overrides: [
      sharedPreferencesProvider.overrideWithValue(prefs),
    ],
  );
}

void main() {
  group('PrefsController', () {
    test('sharedPrefs=null → 退化到 AppPrefs.defaults (system+null locale)',
        () {
      final c = _makeContainer(null);
      addTearDown(c.dispose);
      final s = c.read(prefsControllerProvider);
      expect(s.themeMode, ThemeMode.system);
      expect(s.locale, isNull);
    });

    test('从 SharedPreferences 恢复已保存的 themeMode + locale', () async {
      SharedPreferences.setMockInitialValues({
        'tgpp.prefs.themeMode': 'dark',
        'tgpp.prefs.locale': 'zh',
      });
      final prefs = await SharedPreferences.getInstance();
      final c = _makeContainer(prefs);
      addTearDown(c.dispose);
      final s = c.read(prefsControllerProvider);
      expect(s.themeMode, ThemeMode.dark);
      expect(s.locale, const Locale('zh'));
    });

    test('未知 themeMode 字符串 → 退化到 system', () async {
      SharedPreferences.setMockInitialValues({
        'tgpp.prefs.themeMode': 'sepia',
      });
      final prefs = await SharedPreferences.getInstance();
      final c = _makeContainer(prefs);
      addTearDown(c.dispose);
      expect(c.read(prefsControllerProvider).themeMode, ThemeMode.system);
    });

    test('setThemeMode(dark) 立刻更新 state + 写盘', () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final c = _makeContainer(prefs);
      addTearDown(c.dispose);
      await c.read(prefsControllerProvider.notifier).setThemeMode(ThemeMode.dark);
      expect(c.read(prefsControllerProvider).themeMode, ThemeMode.dark);
      expect(prefs.getString('tgpp.prefs.themeMode'), 'dark');
    });

    test('setLocale(en) 写盘，setLocale(null) 删除 key（= 跟随系统）', () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final c = _makeContainer(prefs);
      addTearDown(c.dispose);
      await c
          .read(prefsControllerProvider.notifier)
          .setLocale(const Locale('en'));
      expect(c.read(prefsControllerProvider).locale, const Locale('en'));
      expect(prefs.getString('tgpp.prefs.locale'), 'en');

      await c.read(prefsControllerProvider.notifier).setLocale(null);
      expect(c.read(prefsControllerProvider).locale, isNull);
      expect(prefs.getString('tgpp.prefs.locale'), isNull);
    });

    test('sharedPrefs=null 时 set* 不抛 + 仅更新内存 state', () async {
      final c = _makeContainer(null);
      addTearDown(c.dispose);
      await c.read(prefsControllerProvider.notifier).setThemeMode(ThemeMode.dark);
      await c
          .read(prefsControllerProvider.notifier)
          .setLocale(const Locale('en'));
      final s = c.read(prefsControllerProvider);
      expect(s.themeMode, ThemeMode.dark);
      expect(s.locale, const Locale('en'));
    });
  });
}
