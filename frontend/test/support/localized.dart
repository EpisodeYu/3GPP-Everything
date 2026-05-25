import 'package:flutter/material.dart';
import 'package:tgpp/core/l10n/app_localizations.dart';

/// 共享的 localizations 配置，所有 widget test 用 [localizedMaterialApp]
/// 或 [localizedMaterialApp.router] 套一层就能拿到 [AppLocalizations.of]。
final appLocalizationsDelegates = AppLocalizations.localizationsDelegates;
final appSupportedLocales = AppLocalizations.supportedLocales;

/// 等价 `MaterialApp(home: ...)`，但默认注入 l10n delegates + supportedLocales。
///
/// 默认 [locale] = `'zh'`，与 M5.0–M5.4 写好的中文断言保持一致；如果测试要校验
/// 英文文案，传 `locale: const Locale('en')`。
MaterialApp localizedMaterialApp({
  Widget? home,
  Locale locale = const Locale('zh'),
}) {
  return MaterialApp(
    home: home,
    locale: locale,
    localizationsDelegates: appLocalizationsDelegates,
    supportedLocales: appSupportedLocales,
  );
}

/// 等价 `MaterialApp.router(routerConfig: ...)` + l10n delegates。
MaterialApp localizedMaterialAppRouter({
  required RouterConfig<Object> routerConfig,
  Locale locale = const Locale('zh'),
}) {
  return MaterialApp.router(
    routerConfig: routerConfig,
    locale: locale,
    localizationsDelegates: appLocalizationsDelegates,
    supportedLocales: appSupportedLocales,
  );
}
