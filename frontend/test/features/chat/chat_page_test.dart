import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/features/chat/chat_page.dart';

import '../../support/fake_auth_controller.dart';
import '../../support/fake_messages_api.dart';
import '../../support/fake_sessions_api.dart';
import '../../support/localized.dart';

Future<({FakeSessionsApi sessions, FakeMessagesApi messages})> _pump(
  WidgetTester tester, {
  required String? sessionId,
  List<SessionOut>? initial,
  FakeMessagesApi? messagesApi,
}) async {
  final sessions = FakeSessionsApi(initial: initial ?? const []);
  final messages = messagesApi ?? FakeMessagesApi();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        fakeAuthControllerOverride,
        sessionsApiProvider.overrideWithValue(sessions),
        messagesApiProvider.overrideWithValue(messages),
      ],
      child: localizedMaterialApp(
        home: Scaffold(body: ChatPage(sessionId: sessionId)),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return (sessions: sessions, messages: messages);
}

void main() {
  testWidgets('sessionId=null → welcome 文案 + "新会话" 按钮', (tester) async {
    await _pump(tester, sessionId: null);
    expect(find.text('开始一个新会话'), findsOneWidget);
    expect(find.byKey(const Key('welcome_new_session')), findsOneWidget);
  });

  testWidgets('welcome "新会话" 按钮触发 SessionsApi.create', (tester) async {
    final h = await _pump(tester, sessionId: null);
    await tester.tap(find.byKey(const Key('welcome_new_session')));
    await tester.pumpAndSettle();
    expect(h.sessions.createCalls, 1);
  });

  testWidgets('sessionId 找不到对应 session：渲染 "找不到该会话" + 回首页按钮',
      (tester) async {
    await _pump(
      tester,
      sessionId: 'sid-unknown',
      initial: [buildSession(id: 'sid-other')],
    );
    expect(find.text('找不到该会话'), findsOneWidget);
    expect(find.byKey(const Key('missing_back_home')), findsOneWidget);
  });

  testWidgets('命中 session：渲染 ChatView 标题 + Composer', (tester) async {
    await _pump(
      tester,
      sessionId: 'sid-3',
      initial: [buildSession(id: 'sid-3', title: 'PDU Session 流程')],
    );
    expect(find.byKey(const Key('chat_header_title')), findsOneWidget);
    expect(find.text('PDU Session 流程'), findsOneWidget);
    expect(find.byKey(const Key('composer_input')), findsOneWidget);
    expect(find.byKey(const Key('composer_send')), findsOneWidget);
  });

  testWidgets('archived_branch 会话：不显示 composer，显示只读 banner', (tester) async {
    await _pump(
      tester,
      sessionId: 'sid-fork',
      initial: [
        buildSession(id: 'sid-fork', title: 'forked', status: 'archived_branch')
      ],
    );
    expect(find.textContaining('只读'), findsOneWidget);
    expect(find.byKey(const Key('composer_input')), findsNothing);
  });

  testWidgets('完整 send → token → final 流：partial 累积 + history 收尾',
      (tester) async {
    final controller = StreamController<ChatEvent>();
    final fakeMsg = FakeMessagesApi()..useLiveStream(controller);
    await _pump(
      tester,
      sessionId: 'sid-9',
      initial: [buildSession(id: 'sid-9', title: 't')],
      messagesApi: fakeMsg,
    );
    expect(find.byKey(const Key('composer_send')), findsOneWidget);

    await tester.enterText(find.byKey(const Key('composer_input')), '你好');
    await tester.pump();
    await tester.tap(find.byKey(const Key('composer_send')));
    await tester.pump();

    // 此时进入 streaming：按钮变成取消
    expect(find.byKey(const Key('composer_cancel')), findsOneWidget);
    expect(find.byKey(const Key('composer_send')), findsNothing);

    controller.add(const RunStartEvent(runId: 'r', sessionId: 'sid-9', messageId: 'm'));
    controller.add(const TokenEvent(delta: 'Hi '));
    controller.add(const TokenEvent(delta: 'there'));
    await tester.pumpAndSettle();
    expect(find.text('Hi there'), findsOneWidget);

    controller.add(const FinalEvent(
      messageId: 'm', answer: 'Hi there', citations: [], confidence: 0.7,
    ));
    controller.add(const EndEvent());
    await controller.close();
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('composer_send')), findsOneWidget);
    expect(find.byKey(const Key('composer_cancel')), findsNothing);
    // history 里应有 user + assistant；assistant 渲染 'Hi there'
    expect(find.text('Hi there'), findsOneWidget);
    expect(find.text('你好'), findsOneWidget);
  });

  testWidgets('取消按钮：触发 MessagesApi.cancelRun 并把状态切到 cancelling',
      (tester) async {
    final controller = StreamController<ChatEvent>();
    final fakeMsg = FakeMessagesApi()..useLiveStream(controller);
    await _pump(
      tester,
      sessionId: 'sid-c',
      initial: [buildSession(id: 'sid-c')],
      messagesApi: fakeMsg,
    );
    await tester.enterText(find.byKey(const Key('composer_input')), 'q');
    await tester.pump();
    await tester.tap(find.byKey(const Key('composer_send')));
    await tester.pump();
    controller.add(const RunStartEvent(runId: 'run-c', sessionId: 'sid-c', messageId: 'm'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('composer_cancel')));
    await tester.pump();
    expect(fakeMsg.lastCancelledRunId, 'run-c');

    controller.add(const CancelledEvent(reason: 'user_cancelled'));
    controller.add(const EndEvent());
    await controller.close();
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('composer_send')), findsOneWidget);
  });

  testWidgets('error event 显示错误 banner', (tester) async {
    final controller = StreamController<ChatEvent>();
    final fakeMsg = FakeMessagesApi()..useLiveStream(controller);
    await _pump(
      tester,
      sessionId: 'sid-e',
      initial: [buildSession(id: 'sid-e')],
      messagesApi: fakeMsg,
    );
    await tester.enterText(find.byKey(const Key('composer_input')), 'q');
    await tester.pump();
    await tester.tap(find.byKey(const Key('composer_send')));
    await tester.pump();
    controller.add(const RunStartEvent(runId: 'r', sessionId: 'sid-e', messageId: 'm'));
    controller.add(const ErrorEvent(code: 'boom', message: 'no'));
    await controller.close();
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('chat_error_banner')), findsOneWidget);
    expect(find.textContaining('boom'), findsOneWidget);
  });
}
