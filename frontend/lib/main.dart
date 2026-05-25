import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/l10n/app_localizations.dart';
import 'core/router.dart';
import 'core/theme.dart';
import 'domain/prefs/prefs_controller.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // M5.6：把 SharedPreferences 注入 provider，PrefsController build() 才能从持久化
  // 恢复主题 / 语言；测试场景没有 override 时退化到默认偏好（见 prefs_controller.dart）。
  final prefs = await SharedPreferences.getInstance();
  runApp(
    ProviderScope(
      overrides: [sharedPreferencesProvider.overrideWithValue(prefs)],
      child: const TgppApp(),
    ),
  );
}

class TgppApp extends ConsumerWidget {
  const TgppApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final router = ref.watch(routerProvider);
    final prefs = ref.watch(prefsControllerProvider);
    return MaterialApp.router(
      title: '3GPP Everything',
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      themeMode: prefs.themeMode,
      locale: prefs.locale,
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      routerConfig: router,
      debugShowCheckedModeBanner: false,
    );
  }
}
