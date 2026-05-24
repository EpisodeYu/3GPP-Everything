import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/features/chat/chat_page.dart';

import '../../support/fake_sessions_api.dart';

Future<FakeSessionsApi> _pump(
  WidgetTester tester, {
  required String? sessionId,
  List<SessionOut>? initial,
}) async {
  final api = FakeSessionsApi(initial: initial ?? const []);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [sessionsApiProvider.overrideWithValue(api)],
      child: MaterialApp(home: Scaffold(body: ChatPage(sessionId: sessionId))),
    ),
  );
  await tester.pumpAndSettle();
  return api;
}

void main() {
  testWidgets('sessionId=null → welcome 文案 + "新会话" 按钮', (tester) async {
    await _pump(tester, sessionId: null);

    expect(find.text('开始一个新会话'), findsOneWidget);
    expect(find.byKey(const Key('welcome_new_session')), findsOneWidget);
  });

  testWidgets('welcome "新会话" 按钮触发 SessionsApi.create', (tester) async {
    final api = await _pump(tester, sessionId: null);

    await tester.tap(find.byKey(const Key('welcome_new_session')));
    await tester.pumpAndSettle();

    expect(api.createCalls, 1);
  });

  testWidgets('sessionId 命中：渲染该 session 标题 + 占位文案', (tester) async {
    await _pump(
      tester,
      sessionId: 'sid-3',
      initial: [buildSession(id: 'sid-3', title: 'PDU Session 流程')],
    );

    expect(find.byKey(const Key('session_placeholder_title')), findsOneWidget);
    expect(find.text('PDU Session 流程'), findsOneWidget);
    expect(find.textContaining('M5.2'), findsOneWidget);
  });

  testWidgets('sessionId 找不到对应 session：渲染 "找不到该会话" + 回首页按钮', (tester) async {
    await _pump(
      tester,
      sessionId: 'sid-unknown',
      initial: [buildSession(id: 'sid-other')],
    );

    expect(find.text('找不到该会话'), findsOneWidget);
    expect(find.byKey(const Key('missing_back_home')), findsOneWidget);
  });
}
