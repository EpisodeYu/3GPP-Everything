import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';
import 'package:tgpp/domain/prefs/prefs_controller.dart';
import 'package:tgpp/features/shell/app_shell.dart';

import '../../support/fake_sessions_api.dart';
import '../../support/localized.dart';

class _StubAuth extends AuthController {
  @override
  Future<AuthState> build() async => AuthAuthenticated(
        Me(
          id: '00000000-0000-0000-0000-000000000001',
          username: 'alice',
          role: 'admin',
          isActive: true,
          createdAt: DateTime.utc(2026, 5, 24),
        ),
      );
}

Future<SharedPreferences> _mockPrefs(Map<String, Object> initial) async {
  SharedPreferences.setMockInitialValues(initial);
  return SharedPreferences.getInstance();
}

GoRouter _shellRouter() {
  return GoRouter(
    initialLocation: '/chat',
    routes: [
      ShellRoute(
        builder: (_, _, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: '/chat',
            builder: (_, _) =>
                const Center(child: Text('content-placeholder')),
          ),
        ],
      ),
    ],
  );
}

Future<void> _pump(
  WidgetTester tester, {
  required SharedPreferences prefs,
  Locale uiLocale = const Locale('zh'),
}) async {
  await tester.binding.setSurfaceSize(const Size(1280, 800));
  addTearDown(() => tester.binding.setSurfaceSize(null));

  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sharedPreferencesProvider.overrideWithValue(prefs),
        sessionsApiProvider
            .overrideWithValue(FakeSessionsApi(initial: const [])),
        authControllerProvider.overrideWith(_StubAuth.new),
      ],
      child: Consumer(builder: (context, ref, _) {
        // MaterialApp.locale 跟 PrefsController 同步，切 locale 后 UI 才会重渲染
        // （实际生产 main.dart 已这么做）。
        final loc = ref.watch(prefsControllerProvider).locale ?? uiLocale;
        return localizedMaterialAppRouter(
          locale: loc,
          routerConfig: _shellRouter(),
        );
      }),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  group('AppShell switcher (theme + language)', () {
    testWidgets('sidebar 同时渲染 theme/language switcher（双 PopupMenuButton）',
        (tester) async {
      final prefs = await _mockPrefs({});
      await _pump(tester, prefs: prefs);
      expect(find.byKey(const Key('theme_switcher')), findsOneWidget);
      expect(find.byKey(const Key('language_switcher')), findsOneWidget);
    });

    testWidgets('点开主题菜单 → 选 dark → PrefsController 切到 dark 并写盘',
        (tester) async {
      final prefs = await _mockPrefs({});
      await _pump(tester, prefs: prefs);

      await tester.tap(find.byKey(const Key('theme_switcher')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('theme_dark')).last);
      await tester.pumpAndSettle();

      expect(prefs.getString('tgpp.prefs.themeMode'), 'dark');
    });

    testWidgets('点开语言菜单 → 选 English → locale 切到 en（UI 文案翻新）',
        (tester) async {
      final prefs = await _mockPrefs({});
      await _pump(tester, prefs: prefs);

      // 切换前看到 zh 默认文案
      expect(find.text('新会话'), findsOneWidget);
      expect(find.text('阅读器'), findsOneWidget);

      await tester.tap(find.byKey(const Key('language_switcher')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('language_en')).last);
      await tester.pumpAndSettle();

      expect(prefs.getString('tgpp.prefs.locale'), 'en');
      expect(find.text('New session'), findsOneWidget);
      expect(find.text('Reader'), findsOneWidget);
    });

    testWidgets('选"跟随系统"语言 → locale 重设为 null（删 key）', (tester) async {
      final prefs = await _mockPrefs({'tgpp.prefs.locale': 'en'});
      await _pump(tester, prefs: prefs);

      await tester.tap(find.byKey(const Key('language_switcher')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('language_system')).last);
      await tester.pumpAndSettle();

      expect(prefs.getString('tgpp.prefs.locale'), isNull);
    });
  });
}
