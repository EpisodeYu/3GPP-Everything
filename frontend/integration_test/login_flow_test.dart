import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:tgpp/core/router.dart';
import 'package:tgpp/core/theme.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';

class _ScriptedAuthController extends AuthController {
  @override
  Future<AuthState> build() async => const AuthAnonymous();

  @override
  Future<void> login({
    required String username,
    required String password,
  }) async {
    state = const AsyncLoading<AuthState>();
    // 给 UI 一次 spinner frame 的机会，更接近真实交互
    await Future<void>.delayed(const Duration(milliseconds: 30));
    state = AsyncData(
      AuthAuthenticated(
        Me(
          id: '00000000-0000-0000-0000-000000000001',
          username: username,
          role: 'admin',
          isActive: true,
          createdAt: DateTime.utc(2026, 5, 24),
        ),
      ),
    );
  }

  @override
  Future<void> logout() async {
    state = const AsyncData(AuthAnonymous());
  }
}

class _ScopedApp extends ConsumerWidget {
  const _ScopedApp();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final router = ref.watch(routerProvider);
    return MaterialApp.router(
      title: '3GPP Everything Smoke',
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      routerConfig: router,
      debugShowCheckedModeBanner: false,
    );
  }
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('login → /chat → logout → /login，端到端在真浏览器跑通',
      (tester) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          authControllerProvider.overrideWith(_ScriptedAuthController.new),
        ],
        child: const _ScopedApp(),
      ),
    );
    await tester.pumpAndSettle();

    // 初始 /chat 触发 redirect → /login
    expect(find.byKey(const Key('login_username')), findsOneWidget,
        reason: '未登录访问 /chat 应被 redirect 到 /login');

    await tester.enterText(find.byKey(const Key('login_username')), 'admin');
    await tester.enterText(
      find.byKey(const Key('login_password')),
      'pw-smoke-12345',
    );
    await tester.tap(find.byKey(const Key('login_submit')));
    await tester.pumpAndSettle();

    expect(find.textContaining('已登录：admin'), findsOneWidget,
        reason: 'login 成功后应 redirect 到 /chat 并显示用户名');
    expect(find.byKey(const Key('logout_button')), findsOneWidget);

    await tester.tap(find.byKey(const Key('logout_button')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('login_username')), findsOneWidget,
        reason: 'logout 后应被 redirect 回 /login');
  });
}
