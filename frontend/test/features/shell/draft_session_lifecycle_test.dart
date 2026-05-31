// "新会话即空草稿" 生命周期 e2e：真 AppShell + 真 ChatPage + GoRouter。
//
// 覆盖：
// - Req2：离开没发过消息的空草稿会话 → 该会话被丢弃（DELETE）。
// - 发过消息后离开 → 会话保留（markUsed 把它移出草稿集）。
// （Req1 复用在 app_shell_test.dart 里用 stub 路由覆盖。）

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:tgpp/data/api/checkpoint_api.dart';
import 'package:tgpp/data/api/favorites_api.dart';
import 'package:tgpp/data/api/feedback_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/data/api/notes_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/features/chat/chat_page.dart';
import 'package:tgpp/features/shell/app_shell.dart';

import '../../support/fake_auth_controller.dart';
import '../../support/fake_checkpoint_api.dart';
import '../../support/fake_favorites_notes_feedback.dart';
import '../../support/fake_messages_api.dart';
import '../../support/fake_sessions_api.dart';
import '../../support/localized.dart';

GoRouter _router() => GoRouter(
      initialLocation: '/chat',
      routes: [
        ShellRoute(
          builder: (_, _, child) => AppShell(child: child),
          routes: [
            GoRoute(path: '/chat', builder: (_, _) => const ChatPage()),
            GoRoute(
              path: '/sessions/:sid',
              builder: (_, s) =>
                  ChatPage(sessionId: s.pathParameters['sid']),
            ),
          ],
        ),
      ],
    );

Future<FakeSessionsApi> _pump(
  WidgetTester tester, {
  List<SessionOut> initialSessions = const [],
}) async {
  await tester.binding.setSurfaceSize(const Size(1280, 800));
  addTearDown(() => tester.binding.setSurfaceSize(null));

  final sessions = FakeSessionsApi(initial: initialSessions);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        fakeAuthControllerOverride,
        sessionsApiProvider.overrideWithValue(sessions),
        messagesApiProvider.overrideWithValue(FakeMessagesApi()),
        checkpointApiProvider.overrideWithValue(FakeCheckpointApi()),
        favoritesApiProvider.overrideWithValue(FakeFavoritesApi()),
        notesApiProvider.overrideWithValue(FakeNotesApi()),
        feedbackApiProvider.overrideWithValue(FakeFeedbackApi()),
      ],
      child: localizedMaterialAppRouter(routerConfig: _router()),
    ),
  );
  await tester.pumpAndSettle();
  return sessions;
}

void main() {
  testWidgets('离开没发过消息的空草稿会话 → 被丢弃（Req2）', (tester) async {
    final api = await _pump(
      tester,
      initialSessions: [buildSession(id: 'a', title: '会话 A')],
    );

    // 建一个空草稿并进入
    await tester.tap(find.byKey(const Key('sidebar_new_session')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('session_tile_fake-001')), findsOneWidget);

    // 切到已有会话 a → 离开空草稿 fake-001
    await tester.tap(find.byKey(const Key('session_tile_a')));
    await tester.pumpAndSettle();

    // fake-001 被丢弃：DELETE 调用 + 从 sidebar 消失
    expect(api.deleteCalls, 1);
    expect(find.byKey(const Key('session_tile_fake-001')), findsNothing);
    expect(find.byKey(const Key('session_tile_a')), findsOneWidget);
  });

  testWidgets('在空草稿里发过消息后离开 → 会话保留（markUsed）', (tester) async {
    final api = await _pump(
      tester,
      initialSessions: [buildSession(id: 'a', title: '会话 A')],
    );

    await tester.tap(find.byKey(const Key('sidebar_new_session')));
    await tester.pumpAndSettle();

    // 发一条消息（onSend → markUsed），把草稿"用掉"
    await tester.enterText(find.byKey(const Key('composer_input')), '你好');
    await tester.pump();
    await tester.tap(find.byKey(const Key('composer_send')));
    await tester.pumpAndSettle();

    // 离开它
    await tester.tap(find.byKey(const Key('session_tile_a')));
    await tester.pumpAndSettle();

    // 不再被当作空草稿丢弃
    expect(api.deleteCalls, 0);
    expect(find.byKey(const Key('session_tile_fake-001')), findsOneWidget);
  });
}
