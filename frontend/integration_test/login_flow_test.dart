import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:tgpp/core/router.dart';
import 'package:tgpp/core/theme.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
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

/// 给 chat-flow smoke 用：固定返回一个 session 让登录后直接进 ChatView。
class _OneSessionApi implements SessionsApi {
  _OneSessionApi(this.session);
  final SessionOut session;

  @override
  Future<SessionListResponse> list({int page = 1, int pageSize = 200}) async =>
      SessionListResponse(items: [session], total: 1);

  @override
  Future<SessionOut> create({String title = '', String modeDefault = 'qa'}) async =>
      session;

  @override
  Future<SessionOut> get(String sid) async => session;

  @override
  Future<SessionOut> patch(String sid, {String? title, String? modeDefault}) async =>
      session;

  @override
  Future<void> delete(String sid) async {}
}

/// Scripted MessagesApi：把单测里调好的事件序列原样吐到 SSE。
class _ScriptedMessagesApi implements MessagesApi {
  _ScriptedMessagesApi(this._controller);

  /// 测试自己 add 事件 / close，从而控制 send → token → final + cancel 的节奏。
  final StreamController<ChatEvent> _controller;

  String? lastCancelledRunId;

  @override
  Future<MessageListResponse> list(String sid, {int page = 1, int pageSize = 200}) async =>
      const MessageListResponse(items: [], total: 0);

  @override
  Stream<ChatEvent> sendMessage(
    String sid,
    SendMessageBody body, {
    dynamic cancelToken,
  }) =>
      _controller.stream;

  @override
  Future<void> cancelRun(String sid, String runId) async {
    lastCancelledRunId = runId;
  }
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

/// 已登录的 AuthController：build() 直接返回 authenticated，跳过 login 页。
class _AuthedController extends AuthController {
  @override
  Future<AuthState> build() async => AuthAuthenticated(
        Me(
          id: '00000000-0000-0000-0000-000000000002',
          username: 'admin',
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

  testWidgets('login → /chat → logout → /login，端到端在真浏览器跑通', (tester) async {
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

  testWidgets('M5.2 chat flow：选会话 → 发问 → 流式 token → 取消 → 收尾',
      (tester) async {
    final session = SessionOut(
      id: 'sess-chat-1',
      userId: 'user-1',
      title: 'PDU Session 流程',
      modeDefault: 'qa',
      status: 'active',
      createdAt: DateTime.utc(2026, 5, 24),
      updatedAt: DateTime.utc(2026, 5, 24),
    );
    final controller = StreamController<ChatEvent>();
    final msgApi = _ScriptedMessagesApi(controller);

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          authControllerProvider.overrideWith(_AuthedController.new),
          sessionsApiProvider.overrideWithValue(_OneSessionApi(session)),
          messagesApiProvider.overrideWithValue(msgApi),
        ],
        child: const _ScopedApp(),
      ),
    );
    await tester.pumpAndSettle();

    final sessionTile = find.byKey(Key('session_tile_${session.id}'));
    expect(sessionTile, findsOneWidget,
        reason: 'sidebar 应展示 _OneSessionApi 返回的那条会话');
    await tester.tap(sessionTile);
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('composer_input')), findsOneWidget);

    // ---- 发 → token 流 → 收一段 → 用户点取消 → CancelledEvent + End ----
    await tester.enterText(find.byKey(const Key('composer_input')), '你好');
    await tester.pump();
    await tester.tap(find.byKey(const Key('composer_send')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('composer_cancel')), findsOneWidget,
        reason: 'send 之后按钮应切到取消');

    controller.add(const RunStartEvent(
      runId: 'run-1', sessionId: 'sess-chat-1', messageId: 'asst-1',
    ));
    controller.add(const TokenEvent(delta: 'Hi '));
    controller.add(const TokenEvent(delta: 'there'));
    await tester.pumpAndSettle();
    expect(find.text('Hi there'), findsOneWidget,
        reason: 'token 累积应即时显示在 assistant 气泡里');

    // 中途取消：用户点取消按钮，应触发 cancelRun(run-1)
    await tester.tap(find.byKey(const Key('composer_cancel')));
    await tester.pumpAndSettle();
    expect(msgApi.lastCancelledRunId, 'run-1');

    // 后端回吐 cancelled + end → 流自然收尾，按钮回到发送
    controller.add(const CancelledEvent(reason: 'user_cancelled'));
    controller.add(const EndEvent());
    await controller.close();
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('composer_send')), findsOneWidget,
        reason: 'cancelled + end 后 composer 回到 idle');
    expect(find.text('你好'), findsOneWidget,
        reason: '用户消息应固化进 history');
    expect(find.text('Hi there'), findsOneWidget,
        reason: 'cancelled 时已累积的 partialAnswer 也应固化');
  });
}
