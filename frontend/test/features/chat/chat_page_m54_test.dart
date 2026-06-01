// M5.4 ChatPage 状态机 widget test：active / paused / archived_branch 三态 + 长按菜单。

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/checkpoint_api.dart';
import 'package:tgpp/data/api/favorites_api.dart';
import 'package:tgpp/data/api/feedback_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/data/api/notes_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/features/chat/chat_page.dart';

import '../../support/fake_auth_controller.dart';
import '../../support/fake_checkpoint_api.dart';
import '../../support/fake_favorites_notes_feedback.dart';
import '../../support/fake_messages_api.dart';
import '../../support/fake_sessions_api.dart';
import '../../support/localized.dart';

class _Pumped {
  _Pumped({
    required this.sessions,
    required this.messages,
    required this.checkpoint,
    required this.favorites,
    required this.notes,
    required this.feedback,
  });

  final FakeSessionsApi sessions;
  final FakeMessagesApi messages;
  final FakeCheckpointApi checkpoint;
  final FakeFavoritesApi favorites;
  final FakeNotesApi notes;
  final FakeFeedbackApi feedback;
}

Future<_Pumped> _pump(
  WidgetTester tester, {
  required String sessionId,
  required List<SessionOut> initial,
  FakeMessagesApi? messagesApi,
  FakeCheckpointApi? checkpointApi,
}) async {
  // 默认 600 太矮：6 个长按菜单 ListTile + bottomSheet drag handle 占 ~620px。
  // 升到 1000 让 menu 全列内一屏显示。
  tester.view.physicalSize = const Size(800, 1000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);

  final sessions = FakeSessionsApi(initial: initial);
  final messages = messagesApi ?? FakeMessagesApi();
  final checkpoint = checkpointApi ?? FakeCheckpointApi();
  final favorites = FakeFavoritesApi();
  final notes = FakeNotesApi();
  final feedback = FakeFeedbackApi();

  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        fakeAuthControllerOverride,
        sessionsApiProvider.overrideWithValue(sessions),
        messagesApiProvider.overrideWithValue(messages),
        checkpointApiProvider.overrideWithValue(checkpoint),
        favoritesApiProvider.overrideWithValue(favorites),
        notesApiProvider.overrideWithValue(notes),
        feedbackApiProvider.overrideWithValue(feedback),
      ],
      child: localizedMaterialApp(
        home: Scaffold(body: ChatPage(sessionId: sessionId)),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return _Pumped(
    sessions: sessions,
    messages: messages,
    checkpoint: checkpoint,
    favorites: favorites,
    notes: notes,
    feedback: feedback,
  );
}

MessageOut _msg({
  required String id,
  required String role,
  required String content,
  String status = 'ok',
}) =>
    MessageOut(
      id: id,
      sessionId: 'sid-active',
      role: role,
      content: content,
      status: status,
      createdAt: DateTime.utc(2026, 5, 24, 20),
    );

void main() {
  group('ChatPage M5.4 / state=active', () {
    testWidgets('streaming：composer 同时显示 暂停 + 取消 双按钮', (tester) async {
      final controller = StreamController<ChatEvent>();
      final messages = FakeMessagesApi()..useLiveStream(controller);
      final h = await _pump(
        tester,
        sessionId: 'sid-active',
        initial: [buildSession(id: 'sid-active', title: 'a')],
        messagesApi: messages,
      );

      await tester.enterText(find.byKey(const Key('composer_input')), 'q');
      await tester.pump();
      await tester.tap(find.byKey(const Key('composer_send')));
      await tester.pump();
      controller.add(const RunStartEvent(
          runId: 'run-x', sessionId: 'sid-active', messageId: 'm'));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('composer_pause')), findsOneWidget);
      expect(find.byKey(const Key('composer_cancel')), findsOneWidget);
      expect(find.byKey(const Key('composer_send')), findsNothing);

      // 点暂停 → 调 CheckpointApi.pause
      await tester.tap(find.byKey(const Key('composer_pause')));
      await tester.pumpAndSettle();
      expect(h.checkpoint.pauseCalls, 1);
      expect(h.checkpoint.lastPauseRunId, 'run-x');

      await controller.close();
      await tester.pumpAndSettle();
    });
  });

  group('ChatPage M5.4 / state=paused', () {
    testWidgets('session.status==paused，controller idle：渲染 paused banner + 恢复按钮',
        (tester) async {
      final h = await _pump(
        tester,
        sessionId: 'sid-paused',
        initial: [
          buildSession(id: 'sid-paused', title: 'p', status: 'paused'),
        ],
      );

      expect(find.byKey(const Key('chat_paused_banner')), findsOneWidget);
      // composer 切到 paused 形态
      expect(find.byKey(const Key('composer_resume')), findsOneWidget);
      expect(find.byKey(const Key('composer_send')), findsNothing);
      // banner 上也有恢复按钮
      expect(find.byKey(const Key('chat_paused_resume')), findsOneWidget);
      // 验证 fake state
      expect(h.checkpoint.resumeCalls, 0);
    });

    testWidgets('点 banner 上的恢复按钮 → 调 CheckpointApi.resume', (tester) async {
      final resumeStream = StreamController<ChatEvent>();
      final checkpoint = FakeCheckpointApi()..useLiveStream(resumeStream);
      final h = await _pump(
        tester,
        sessionId: 'sid-paused',
        initial: [
          buildSession(id: 'sid-paused', title: 'p', status: 'paused'),
        ],
        checkpointApi: checkpoint,
      );

      await tester.tap(find.byKey(const Key('chat_paused_resume')));
      await tester.pump();
      // resume 触发后 FakeCheckpointApi.resumeCalls 增长
      await tester.pump(const Duration(milliseconds: 50));
      expect(h.checkpoint.resumeCalls, 1);

      // 模拟 final + end → 流结束
      resumeStream.add(const FinalEvent(
        messageId: 'm0',
        answer: 'resumed answer',
        citations: [],
        confidence: 0.5,
      ));
      resumeStream.add(const EndEvent());
      await resumeStream.close();
      await tester.pumpAndSettle();
    });
  });

  group('ChatPage M5.4 / state=archived_branch', () {
    testWidgets('archived_branch：composer 不显示，只读 banner + "回到主线" 按钮',
        (tester) async {
      await _pump(
        tester,
        sessionId: 'sid-arch',
        initial: [
          buildSession(
            id: 'sid-arch',
            title: 'old',
            status: 'archived_branch',
            forkedFromSessionId: 'sid-main',
          ),
          buildSession(id: 'sid-main', title: '主线'),
        ],
      );

      expect(find.byKey(const Key('composer_input')), findsNothing);
      expect(find.byKey(const Key('chat_archived_banner')), findsOneWidget);
      expect(find.byKey(const Key('chat_archived_back_to_main')), findsOneWidget);
    });

    testWidgets('archived_branch 但无 forkedFromSessionId：banner 不显示"回到主线"',
        (tester) async {
      await _pump(
        tester,
        sessionId: 'sid-arch2',
        initial: [
          buildSession(id: 'sid-arch2', status: 'archived_branch'),
        ],
      );
      expect(find.byKey(const Key('chat_archived_banner')), findsOneWidget);
      expect(find.byKey(const Key('chat_archived_back_to_main')), findsNothing);
    });

    testWidgets('archived_branch：header 不显示删除最后 N 轮入口（rollback 被禁用）',
        (tester) async {
      await _pump(
        tester,
        sessionId: 'sid-arch3',
        initial: [
          buildSession(id: 'sid-arch3', status: 'archived_branch'),
        ],
      );
      expect(find.byKey(const Key('chat_header_settings')), findsNothing);
    });
  });

  group('ChatPage M5.4 / rollback (header settings)', () {
    testWidgets('点 header 设置 → 删除最后 N 轮 → slider + 确认 → 调 rollback',
        (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'u', role: 'user', content: 'q'),
        _msg(id: 'a', role: 'assistant', content: 'ans'),
      ]);
      final checkpoint = FakeCheckpointApi(
        rollbackResponse:
            const RollbackResponse(deletedMessages: 2, headCheckpointId: 'h'),
      );
      final h = await _pump(
        tester,
        sessionId: 'sid-rb',
        initial: [buildSession(id: 'sid-rb', title: 'rb')],
        messagesApi: messages,
        checkpointApi: checkpoint,
      );

      // 打开 settings 菜单
      await tester.tap(find.byKey(const Key('chat_header_settings')));
      await tester.pumpAndSettle();
      // 点 "删除最后 N 轮"
      await tester.tap(find.text('删除最后 N 轮'));
      await tester.pumpAndSettle();
      // dialog 出现 → 直接确认（默认 N=1）
      expect(find.byKey(const Key('rollback_confirm')), findsOneWidget);
      // 模拟 rollback 后 history 被清空
      messages.history = [];
      await tester.tap(find.byKey(const Key('rollback_confirm')));
      await tester.pumpAndSettle();

      expect(h.checkpoint.rollbackCalls, 1);
      expect(h.checkpoint.lastRollbackLastN, 1);
      expect(find.text('已删除最后 1 轮（共 2 条消息）'), findsOneWidget);
    });
  });

  group('ChatPage M5.4 / 长按菜单', () {
    testWidgets('长按 user 消息 → 弹菜单 → 点 "从这里重问" → 弹 fork dialog → 调 fork',
        (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'u1', role: 'user', content: '老问题'),
      ]);
      final checkpoint = FakeCheckpointApi(
        checkpoints: [buildCheckpoint(checkpointId: 'cp-1')],
      );
      final h = await _pump(
        tester,
        sessionId: 'sid-fk',
        initial: [buildSession(id: 'sid-fk', title: 'fk')],
        messagesApi: messages,
        checkpointApi: checkpoint,
      );

      await tester.longPress(find.byKey(const ValueKey('msg-u1')));
      await tester.pumpAndSettle();
      // 用户菜单出现
      expect(find.byKey(const Key('user_menu_fork')), findsOneWidget);
      await tester.tap(find.byKey(const Key('user_menu_fork')));
      await tester.pumpAndSettle();
      // fork dialog 出现，输入框预填了原问题
      expect(find.byKey(const Key('fork_input')), findsOneWidget);
      await tester.enterText(find.byKey(const Key('fork_input')), '换个问法');
      await tester.tap(find.byKey(const Key('fork_confirm')));
      await tester.pumpAndSettle();

      expect(h.checkpoint.forkCalls, 1);
      expect(h.checkpoint.lastForkCheckpointId, 'cp-1');
      expect(h.checkpoint.lastForkNewUserMessage, '换个问法');
    });

    testWidgets('长按 assistant 消息 → 复制：触发 SnackBar', (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'a1', role: 'assistant', content: 'PDU 流程是 ...'),
      ]);
      await _pump(
        tester,
        sessionId: 'sid-c',
        initial: [buildSession(id: 'sid-c')],
        messagesApi: messages,
      );

      await tester.longPress(find.byKey(const ValueKey('msg-a1')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('assistant_menu_copy')), findsOneWidget);
      await tester.tap(find.byKey(const Key('assistant_menu_copy')));
      await tester.pumpAndSettle();
      expect(find.text('已复制消息'), findsOneWidget);
    });

    testWidgets('长按 assistant → 点赞：调 feedback API thumb=1', (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'a1', role: 'assistant', content: 'ok'),
      ]);
      final h = await _pump(
        tester,
        sessionId: 'sid-f',
        initial: [buildSession(id: 'sid-f')],
        messagesApi: messages,
      );

      await tester.longPress(find.byKey(const ValueKey('msg-a1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('assistant_menu_thumb_up')));
      await tester.pumpAndSettle();

      expect(h.feedback.upsertCalls, 1);
      expect(h.feedback.lastMessageId, 'a1');
      expect(h.feedback.lastThumb, 1);
      expect(find.text('已点赞'), findsOneWidget);
    });

    testWidgets('长按 assistant → 收藏：调 favorites.create target_type=message',
        (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'a-fav', role: 'assistant', content: 'ok'),
      ]);
      final h = await _pump(
        tester,
        sessionId: 'sid-fav',
        initial: [buildSession(id: 'sid-fav')],
        messagesApi: messages,
      );

      await tester.longPress(find.byKey(const ValueKey('msg-a-fav')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('assistant_menu_favorite')));
      await tester.pumpAndSettle();

      expect(h.favorites.createCalls, 1);
      expect(h.favorites.lastTargetType, 'message');
      expect(h.favorites.lastTargetId, 'a-fav');
      expect(find.text('已收藏'), findsOneWidget);
    });

    testWidgets('长按 assistant → 笔记 → dialog 输入 → 调 notes.create', (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'a-note', role: 'assistant', content: 'ok'),
      ]);
      final h = await _pump(
        tester,
        sessionId: 'sid-note',
        initial: [buildSession(id: 'sid-note')],
        messagesApi: messages,
      );

      await tester.longPress(find.byKey(const ValueKey('msg-a-note')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('assistant_menu_note')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('note_input')), '这是关键引用');
      await tester.tap(find.byKey(const Key('note_confirm')));
      await tester.pumpAndSettle();

      expect(h.notes.createCalls, 1);
      expect(h.notes.lastTargetType, 'message');
      expect(h.notes.lastTargetId, 'a-note');
      expect(h.notes.lastBody, '这是关键引用');
    });

    testWidgets('长按 assistant → 详细反馈 → dialog → 选 踩 + 写理由 → 调 feedback',
        (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'a-fb', role: 'assistant', content: 'ok'),
      ]);
      final h = await _pump(
        tester,
        sessionId: 'sid-fb',
        initial: [buildSession(id: 'sid-fb')],
        messagesApi: messages,
      );

      await tester.longPress(find.byKey(const ValueKey('msg-a-fb')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('assistant_menu_feedback')));
      // Bottom sheet pop 后 _openFeedbackDialog 才 await showDialog；多 pump
      // 一轮让 dialog 落帧（pumpAndSettle 已包含动画但 microtask 链分两步）
      await tester.pumpAndSettle();
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('feedback_thumb_down')));
      await tester.pump();
      await tester.enterText(find.byKey(const Key('feedback_reason')), '答错了');
      await tester.tap(find.byKey(const Key('feedback_confirm')));
      await tester.pumpAndSettle();

      expect(h.feedback.upsertCalls, 1);
      expect(h.feedback.lastThumb, -1);
      expect(h.feedback.lastReason, '答错了');
      expect(find.text('反馈已提交'), findsOneWidget);
    });

    testWidgets('archived_branch 会话上长按消息：菜单不弹出（只读）', (tester) async {
      final messages = FakeMessagesApi(history: [
        _msg(id: 'a-r', role: 'assistant', content: 'old'),
      ]);
      await _pump(
        tester,
        sessionId: 'sid-arx',
        initial: [
          buildSession(id: 'sid-arx', status: 'archived_branch'),
        ],
        messagesApi: messages,
      );
      // archived_branch 下 MessageBubble 没被 GestureDetector 包裹（只读），
      // 故 longPress 的 hit test 落在背景。warnIfMissed:false 静默警告。
      await tester.longPress(
        find.byKey(const ValueKey('msg-a-r')),
        warnIfMissed: false,
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('assistant_menu_copy')), findsNothing);
    });
  });
}
