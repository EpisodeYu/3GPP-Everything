import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/admin_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/features/admin/admin_session_view.dart';
import 'package:tgpp/features/reader/widgets/highlight_overlay.dart';

import '../../support/fake_admin_api.dart';
import '../../support/localized.dart';

MessageOut _msg(String id, String role, String content) => MessageOut(
      id: id,
      sessionId: 's-1',
      role: role,
      content: content,
      status: 'ok',
      createdAt: DateTime.utc(2026, 5, 24, 20),
    );

Future<FakeAdminApi> _pump(
  WidgetTester tester, {
  required FakeAdminApi admin,
  String? highlightMessageId,
}) async {
  await tester.pumpWidget(
    ProviderScope(
      overrides: [adminApiProvider.overrideWithValue(admin)],
      child: localizedMaterialApp(
        home: AdminSessionView(
          sessionId: 's-1',
          highlightMessageId: highlightMessageId,
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return admin;
}

void main() {
  group('AdminSessionView', () {
    testWidgets('渲染会话元信息 + 全部消息，高亮目标', (tester) async {
      final admin = FakeAdminApi()
        ..setSessionDetail(AdminSessionDetailOut(
          id: 's-1',
          title: 'u1 的会话',
          username: 'u1',
          createdAt: '2026-05-24T20:00:00Z',
          messages: [
            _msg('m-1', 'user', '什么是 PDCP'),
            _msg('m-2', 'assistant', 'PDCP 是…'),
          ],
        ));
      await _pump(tester, admin: admin, highlightMessageId: 'm-2');

      expect(admin.lastSessionDetailSid, 's-1');
      expect(find.text('u1 的会话'), findsOneWidget);
      expect(find.byKey(const ValueKey('admin-msg-m-1')), findsOneWidget);
      expect(find.byKey(const ValueKey('admin-msg-m-2')), findsOneWidget);
      expect(find.byType(HighlightOverlay), findsOneWidget);
      expect(
        find.descendant(
          of: find.byType(HighlightOverlay),
          matching: find.byKey(const ValueKey('admin-msg-m-2')),
        ),
        findsOneWidget,
      );
    });

    testWidgets('无 highlight → 不包裹高亮', (tester) async {
      final admin = FakeAdminApi()
        ..setSessionDetail(AdminSessionDetailOut(
          id: 's-1',
          title: 't',
          username: 'u1',
          createdAt: '2026-05-24T20:00:00Z',
          messages: [_msg('m-1', 'user', 'hi')],
        ));
      await _pump(tester, admin: admin);
      expect(find.byType(HighlightOverlay), findsNothing);
    });

    testWidgets('加载失败 → 显示错误 + 重试', (tester) async {
      final admin = FakeAdminApi()..sessionDetailErr = StateError('boom');
      await _pump(tester, admin: admin);
      expect(find.byKey(const Key('admin_session_error')), findsOneWidget);
    });
  });
}
