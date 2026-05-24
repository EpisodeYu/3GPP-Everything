import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:tgpp/core/router.dart';
import 'package:tgpp/core/theme.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';

/// In-memory SessionsApi 占位，避免 web smoke 跑出去打真后端。
class _EmptySessionsApi implements SessionsApi {
  @override
  Future<SessionListResponse> list({int page = 1, int pageSize = 200}) async =>
      const SessionListResponse(items: [], total: 0);

  @override
  Future<SessionOut> create({String title = '', String modeDefault = 'qa'}) async {
    final now = DateTime.utc(2026, 5, 24);
    return SessionOut(
      id: 'smoke-1',
      userId: 'user-smoke',
      title: title,
      modeDefault: modeDefault,
      status: 'active',
      createdAt: now,
      updatedAt: now,
    );
  }

  @override
  Future<SessionOut> get(String sid) async => throw UnimplementedError();

  @override
  Future<SessionOut> patch(String sid, {String? title, String? modeDefault}) async =>
      throw UnimplementedError();

  @override
  Future<void> delete(String sid) async {}
}

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
          sessionsApiProvider.overrideWithValue(_EmptySessionsApi()),
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

    // M5.1 起 logout 入口挪到 AppShell sidebar；登录态用 welcome 文案 + sidebar
    // 上的 username 双锚点确认（避免对单一文案过拟合）。
    expect(find.text('开始一个新会话'), findsOneWidget,
        reason: 'login 成功后应 redirect 到 /chat welcome 占位页');
    expect(find.text('admin'), findsWidgets,
        reason: 'sidebar 底部应显示用户名');
    expect(find.byKey(const Key('sidebar_logout')), findsOneWidget);

    await tester.tap(find.byKey(const Key('sidebar_logout')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('login_username')), findsOneWidget,
        reason: 'logout 后应被 redirect 回 /login');
  });
}
