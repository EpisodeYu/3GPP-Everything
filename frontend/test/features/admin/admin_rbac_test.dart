import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';
import 'package:tgpp/features/shell/app_shell.dart';

import '../../support/fake_sessions_api.dart';

/// RBAC widget test：sidebar 上 "管理后台" 入口对 admin 可见、对普通 user 隐藏。
///
/// 锚：`docs/03-development/05-frontend.md §0 M5.5 / §7`。第二道防线（后端 403）
/// 不在此处覆盖，由 `data/api/admin_api_test.dart 403 case` 兜底。
class _StubAuthControllerAdmin extends AuthController {
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

class _StubAuthControllerUser extends AuthController {
  @override
  Future<AuthState> build() async => AuthAuthenticated(
        Me(
          id: '00000000-0000-0000-0000-000000000002',
          username: 'bob',
          role: 'user',
          isActive: true,
          createdAt: DateTime.utc(2026, 5, 24),
        ),
      );
}

GoRouter _adminTestRouter({String initial = '/chat'}) {
  return GoRouter(
    initialLocation: initial,
    routes: [
      ShellRoute(
        builder: (_, _, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: '/chat',
            builder: (_, _) => const Center(child: Text('chat-placeholder')),
          ),
          GoRoute(
            path: '/admin',
            builder: (_, _) =>
                const Center(child: Text('admin-placeholder')),
          ),
        ],
      ),
    ],
    redirect: (_, state) {
      // 在测试里复刻 core/router.dart 的 RBAC 规则；要让 user 走 /admin → /chat。
      // 这里没有真 ProviderScope 访问，所以我们直接用 initial 的语义即可。
      return null;
    },
  );
}

Future<void> _pump(
  WidgetTester tester, {
  required AuthController Function() ctor,
  String initial = '/chat',
}) async {
  final api = FakeSessionsApi(initial: const []);
  await tester.binding.setSurfaceSize(const Size(1280, 800));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionsApiProvider.overrideWithValue(api),
        authControllerProvider.overrideWith(ctor),
      ],
      child: MaterialApp.router(
        routerConfig: _adminTestRouter(initial: initial),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  group('AppShell admin entry RBAC', () {
    testWidgets('admin 看见 "管理后台" 入口，点击跳到 /admin', (tester) async {
      await _pump(tester, ctor: _StubAuthControllerAdmin.new);

      final entry = find.byKey(const Key('sidebar_open_admin'));
      expect(entry, findsOneWidget);

      await tester.tap(entry);
      await tester.pumpAndSettle();

      expect(find.text('admin-placeholder'), findsOneWidget);
    });

    testWidgets('普通 user 不渲染 "管理后台" 入口', (tester) async {
      await _pump(tester, ctor: _StubAuthControllerUser.new);

      expect(find.byKey(const Key('sidebar_open_admin')), findsNothing);
      // sidebar 其他入口仍在
      expect(find.byKey(const Key('sidebar_new_session')), findsOneWidget);
      expect(find.byKey(const Key('sidebar_open_reader')), findsOneWidget);
    });
  });
}
