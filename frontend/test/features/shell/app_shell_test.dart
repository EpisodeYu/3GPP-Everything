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
import '../../support/localized.dart';

class _StubAuthController extends AuthController {
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

  @override
  Future<void> logout() async {
    state = const AsyncData(AuthAnonymous());
  }
}

GoRouter _router({String initial = '/sessions/a'}) {
  return GoRouter(
    initialLocation: initial,
    routes: [
      ShellRoute(
        builder: (_, _, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: '/chat',
            builder: (_, _) =>
                const Center(child: Text('chat-placeholder')),
          ),
          GoRoute(
            path: '/sessions/:sid',
            builder: (_, s) => Center(
              child: Text('session-${s.pathParameters['sid']}'),
            ),
          ),
        ],
      ),
    ],
  );
}

Future<FakeSessionsApi> _pumpShell(
  WidgetTester tester, {
  required Size size,
  List<SessionOut>? initialSessions,
  String initialRoute = '/sessions/a',
}) async {
  final api = FakeSessionsApi(
    initial: initialSessions ??
        [
          buildSession(id: 'a', title: '会话 A'),
          buildSession(id: 'b', title: '会话 B'),
        ],
  );
  await tester.binding.setSurfaceSize(size);
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        sessionsApiProvider.overrideWithValue(api),
        authControllerProvider.overrideWith(_StubAuthController.new),
      ],
      child: localizedMaterialAppRouter(
        routerConfig: _router(initial: initialRoute),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return api;
}

void main() {
  group('AppShell 响应式布局', () {
    testWidgets('宽屏 (>= 840) 固定 sidebar，无 Drawer', (tester) async {
      await _pumpShell(tester, size: const Size(1280, 800));

      // sidebar 内的 "新会话" 按钮直接可见
      expect(find.byKey(const Key('sidebar_new_session')), findsOneWidget);
      // 会话列表渲染了 2 个 tile
      expect(find.byKey(const Key('session_tile_a')), findsOneWidget);
      expect(find.byKey(const Key('session_tile_b')), findsOneWidget);
      // 没有 AppBar / Drawer
      expect(find.byType(AppBar), findsNothing);
      expect(find.byType(Drawer), findsNothing);
    });

    // 锚：2026-05-25 "全站文字可选中复制"。AppShell 把路由主内容包进 SelectionArea，
    // 让 Flutter web（CanvasKit）下的 Text/Markdown 支持鼠标拖选 + 复制。
    testWidgets('路由主内容被包进 SelectionArea（全站可选中）', (tester) async {
      await _pumpShell(tester, size: const Size(1280, 800));

      final content = find.text('session-a');
      expect(content, findsOneWidget);
      expect(
        find.ancestor(of: content, matching: find.byType(SelectionArea)),
        findsOneWidget,
        reason: '路由内容必须在 SelectionArea 之下才支持选中复制',
      );
    });

    testWidgets('窄屏 (< 840) 渲染 AppBar + Drawer，打开 drawer 才能看到 sidebar 内容',
        (tester) async {
      await _pumpShell(tester, size: const Size(480, 800));

      expect(find.byType(AppBar), findsOneWidget);
      // 抽屉默认收起，"新会话"按钮不在树里
      expect(find.byKey(const Key('sidebar_new_session')), findsNothing);

      // 打开 drawer
      final state = tester.state<ScaffoldState>(find.byType(Scaffold).first);
      state.openDrawer();
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('sidebar_new_session')), findsOneWidget);
      expect(find.byKey(const Key('session_tile_a')), findsOneWidget);
    });
  });

  group('AppShell 会话操作', () {
    testWidgets('点击 sidebar tile 切换 location 到 /sessions/{sid}',
        (tester) async {
      await _pumpShell(tester, size: const Size(1280, 800));

      expect(find.text('session-a'), findsOneWidget);

      await tester.tap(find.byKey(const Key('session_tile_b')));
      await tester.pumpAndSettle();

      expect(find.text('session-b'), findsOneWidget);
      expect(find.text('session-a'), findsNothing);
    });

    testWidgets('"新会话" 触发 API 并跳到新 session', (tester) async {
      final api = await _pumpShell(
        tester,
        size: const Size(1280, 800),
        initialSessions: const [],
        initialRoute: '/chat',
      );

      expect(find.text('chat-placeholder'), findsOneWidget);

      await tester.tap(find.byKey(const Key('sidebar_new_session')));
      await tester.pumpAndSettle();

      expect(api.createCalls, 1);
      // FakeSessionsApi 返回 id=fake-001
      expect(find.text('session-fake-001'), findsOneWidget);
    });

    testWidgets('删除会话：确认后调 delete，并把当前选中态跳回 /chat', (tester) async {
      final api = await _pumpShell(tester, size: const Size(1280, 800));

      await tester.tap(find.byKey(const Key('session_menu_a')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('delete_confirm')));
      await tester.pumpAndSettle();

      expect(api.deleteCalls, 1);
      expect(find.byKey(const Key('session_tile_a')), findsNothing);
      expect(find.text('chat-placeholder'), findsOneWidget);
    });

    testWidgets('archived_branch 分组单独排到底部带 "分叉历史" 标签', (tester) async {
      await _pumpShell(
        tester,
        size: const Size(1280, 800),
        initialSessions: [
          buildSession(id: 'a', title: '主线'),
          buildSession(id: 'arc', title: '历史分支', status: 'archived_branch'),
        ],
      );

      expect(find.text('分叉历史'), findsOneWidget);
      // 两个 tile 都存在
      expect(find.byKey(const Key('session_tile_a')), findsOneWidget);
      expect(find.byKey(const Key('session_tile_arc')), findsOneWidget);
    });

    testWidgets('退出登录通过 sidebar 入口触发', (tester) async {
      await _pumpShell(tester, size: const Size(1280, 800));

      expect(find.text('alice'), findsOneWidget);
      await tester.tap(find.byKey(const Key('sidebar_logout')));
      await tester.pumpAndSettle();
      // logout 改 auth state，AppShell 内的 username 应消失（变成 "-"）
      expect(find.text('alice'), findsNothing);
    });

    testWidgets('一键清空：确认后调 deleteAll、列表清空、当前选中态回 /chat',
        (tester) async {
      final api = await _pumpShell(tester, size: const Size(1280, 800));

      expect(find.byKey(const Key('sidebar_delete_all')), findsOneWidget);
      await tester.tap(find.byKey(const Key('sidebar_delete_all')));
      await tester.pumpAndSettle();

      // 二次确认对话框
      expect(find.byKey(const Key('delete_all_dialog')), findsOneWidget);
      await tester.tap(find.byKey(const Key('delete_all_confirm')));
      await tester.pumpAndSettle();

      expect(api.deleteAllCalls, 1);
      expect(find.byKey(const Key('session_tile_a')), findsNothing);
      expect(find.byKey(const Key('session_tile_b')), findsNothing);
      // sessions 清空后按钮自动隐藏（避免空列表下 UI 噪声）
      expect(find.byKey(const Key('sidebar_delete_all')), findsNothing);
      // 当前停在 /sessions/a → deleteAll 成功后跳回 /chat
      expect(find.text('chat-placeholder'), findsOneWidget);
    });

    testWidgets('一键清空：取消对话框不触发 API', (tester) async {
      final api = await _pumpShell(tester, size: const Size(1280, 800));

      await tester.tap(find.byKey(const Key('sidebar_delete_all')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('delete_all_cancel')));
      await tester.pumpAndSettle();

      expect(api.deleteAllCalls, 0);
      expect(find.byKey(const Key('session_tile_a')), findsOneWidget);
    });

    testWidgets('空会话列表时不显示"清空全部"按钮', (tester) async {
      await _pumpShell(
        tester,
        size: const Size(1280, 800),
        initialSessions: const [],
        initialRoute: '/chat',
      );
      expect(find.byKey(const Key('sidebar_delete_all')), findsNothing);
    });
  });
}
