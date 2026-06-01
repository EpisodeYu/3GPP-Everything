import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/checkpoint_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/features/chat/chat_controller.dart';

import '../../support/fake_auth_controller.dart';
import '../../support/fake_checkpoint_api.dart';
import '../../support/fake_messages_api.dart';
import '../../support/fake_sessions_api.dart';

ProviderContainer _container({
  required FakeMessagesApi messages,
  required FakeCheckpointApi checkpoint,
}) {
  final c = ProviderContainer(overrides: [
    fakeAuthControllerOverride,
    messagesApiProvider.overrideWithValue(messages),
    checkpointApiProvider.overrideWithValue(checkpoint),
    // build() 会读 sessionsControllerProvider.isDraft；注入 fake 避免真 dio。
    sessionsApiProvider.overrideWithValue(FakeSessionsApi()),
  ]);
  addTearDown(c.dispose);
  return c;
}

ProviderSubscription<AsyncValue<SessionChatState>> _keepAlive(
    ProviderContainer c, String sid) {
  final sub = c.listen<AsyncValue<SessionChatState>>(
    chatControllerProvider(sid),
    (_, _) {},
  );
  addTearDown(sub.close);
  return sub;
}

Future<SessionChatState> _waitUntil(
  ProviderContainer c,
  String sid,
  bool Function(SessionChatState) ready, {
  Duration timeout = const Duration(seconds: 2),
}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    final s = c.read(chatControllerProvider(sid)).value;
    if (s != null && ready(s)) return s;
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
  fail('state did not reach expected condition within $timeout');
}

void main() {
  const sid = 'session-cp';

  group('ChatController pause/resume', () {
    test('pause() 流中 → 调 checkpoint API + 状态切到 paused，stream onDone 不再标 error',
        () async {
      final messages = FakeMessagesApi();
      final streamCtrl = StreamController<ChatEvent>();
      messages.useLiveStream(streamCtrl);
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);

      await c.read(chatControllerProvider(sid).future);
      unawaited(c.read(chatControllerProvider(sid).notifier).send('hi'));
      streamCtrl
          .add(const RunStartEvent(runId: 'run-p', sessionId: sid, messageId: 'm'));
      streamCtrl.add(const TokenEvent(delta: 'partial'));
      await _waitUntil(c, sid, (s) => s.run.runId == 'run-p');

      await c.read(chatControllerProvider(sid).notifier).pause();

      expect(ckpt.pauseCalls, 1);
      expect(ckpt.lastPauseRunId, 'run-p');
      final paused = c.read(chatControllerProvider(sid)).value!;
      expect(paused.run.status, RunStatus.paused);
      expect(paused.run.partialAnswer, 'partial');
      expect(paused.run.runId, 'run-p');

      // 后端在 pause 后会自然关流：模拟 onDone 不应该标 error
      await streamCtrl.close();
      await Future<void>.delayed(const Duration(milliseconds: 30));
      final after = c.read(chatControllerProvider(sid)).value!;
      expect(after.run.status, RunStatus.paused);
      expect(after.run.errorMessage, isNull);
    });

    test('pause() 在非 streaming 状态下是 no-op', () async {
      final messages = FakeMessagesApi();
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);

      await c.read(chatControllerProvider(sid).notifier).pause();
      expect(ckpt.pauseCalls, 0);
      expect(c.read(chatControllerProvider(sid)).value!.run.status,
          RunStatus.idle);
    });

    test('resume() 从 paused 启动新 SSE 流；final 后从 PG refetch history', () async {
      final messages = FakeMessagesApi();
      final streamCtrl = StreamController<ChatEvent>();
      messages.useLiveStream(streamCtrl);
      final resumeStream = StreamController<ChatEvent>();
      final ckpt = FakeCheckpointApi()..useLiveStream(resumeStream);
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);

      await c.read(chatControllerProvider(sid).future);
      unawaited(c.read(chatControllerProvider(sid).notifier).send('q'));
      streamCtrl
          .add(const RunStartEvent(runId: 'r0', sessionId: sid, messageId: 'm0'));
      streamCtrl.add(const TokenEvent(delta: 'foo '));
      await _waitUntil(c, sid, (s) => s.run.partialAnswer == 'foo ');
      await c.read(chatControllerProvider(sid).notifier).pause();
      await streamCtrl.close();
      expect(c.read(chatControllerProvider(sid)).value!.run.status,
          RunStatus.paused);

      // resume 走新的 stream（FakeCheckpointApi.useLiveStream）
      messages.history = [
        MessageOut(
          id: 'user-0',
          sessionId: sid,
          role: 'user',
          content: 'q',
          status: 'ok',
          createdAt: DateTime.utc(2026, 5, 24, 20),
        ),
        MessageOut(
          id: 'asst-0',
          sessionId: sid,
          role: 'assistant',
          content: 'foo bar',
          status: 'ok',
          createdAt: DateTime.utc(2026, 5, 24, 20, 1),
        ),
      ];

      unawaited(c.read(chatControllerProvider(sid).notifier).resume());
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.streaming);
      resumeStream.add(const TokenEvent(delta: 'bar'));
      resumeStream.add(const FinalEvent(
        messageId: 'm0', answer: 'foo bar', citations: [], confidence: 0.7,
      ));
      resumeStream.add(const EndEvent());
      await resumeStream.close();

      final after =
          await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
      // resume 走 PG refetch 路径
      expect(after.history.length, 2);
      expect(after.history[1].content, 'foo bar');
      expect(ckpt.resumeCalls, 1);
    });

    test('resume() 从 streaming/cancelling 状态拒绝（避免冲突）', () async {
      final messages = FakeMessagesApi();
      final streamCtrl = StreamController<ChatEvent>();
      messages.useLiveStream(streamCtrl);
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);

      await c.read(chatControllerProvider(sid).future);
      unawaited(c.read(chatControllerProvider(sid).notifier).send('q'));
      streamCtrl
          .add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.streaming);

      await c.read(chatControllerProvider(sid).notifier).resume();
      expect(ckpt.resumeCalls, 0);

      streamCtrl.add(const FinalEvent(
        messageId: 'm', answer: 'a', citations: [], confidence: 0,
      ));
      streamCtrl.add(const EndEvent());
      await streamCtrl.close();
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
    });
  });

  group('ChatController rollback', () {
    test('rollback() 调 API + 重新从 PG 加载 history', () async {
      final messages = FakeMessagesApi(history: [
        MessageOut(
          id: 'u', sessionId: sid, role: 'user', content: 'q', status: 'ok',
          createdAt: DateTime.utc(2026, 5, 24),
        ),
      ]);
      final ckpt = FakeCheckpointApi(
        rollbackResponse:
            const RollbackResponse(deletedMessages: 2, headCheckpointId: 'h'),
      );
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);

      // 模拟 rollback 后 PG 已没有那条 user msg
      messages.history = [];
      final resp =
          await c.read(chatControllerProvider(sid).notifier).rollback(2);
      expect(resp.deletedMessages, 2);
      expect(ckpt.lastRollbackLastN, 2);
      expect(c.read(chatControllerProvider(sid)).value!.history, isEmpty);
    });

    test('rollback() 在跑中 run 时抛 StateError', () async {
      final messages = FakeMessagesApi();
      final streamCtrl = StreamController<ChatEvent>();
      messages.useLiveStream(streamCtrl);
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);
      unawaited(c.read(chatControllerProvider(sid).notifier).send('q'));
      streamCtrl
          .add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.streaming);

      await expectLater(
        c.read(chatControllerProvider(sid).notifier).rollback(1),
        throwsA(isA<StateError>()),
      );
      expect(ckpt.rollbackCalls, 0);

      streamCtrl.add(const FinalEvent(
        messageId: 'm', answer: 'a', citations: [], confidence: 0,
      ));
      streamCtrl.add(const EndEvent());
      await streamCtrl.close();
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
    });

    test('listCheckpoints() 透传给 checkpoint API', () async {
      final messages = FakeMessagesApi();
      final ckpt = FakeCheckpointApi(checkpoints: [
        buildCheckpoint(checkpointId: 'cp-A'),
        buildCheckpoint(checkpointId: 'cp-B'),
      ]);
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);

      final list =
          await c.read(chatControllerProvider(sid).notifier).listCheckpoints();
      expect(list.items.map((e) => e.checkpointId).toList(), ['cp-A', 'cp-B']);
      expect(ckpt.listCalls, 1);
    });
  });

  group('ChatController editLastTurn', () {
    /// 构造一对完整 user+assistant 的初始 history（"上一轮"已结束）。
    List<MessageOut> initialPair() => [
          MessageOut(
            id: 'u-old',
            sessionId: sid,
            role: 'user',
            content: 'wrong question',
            status: 'ok',
            createdAt: DateTime.utc(2026, 6, 1, 10),
          ),
          MessageOut(
            id: 'a-old',
            sessionId: sid,
            role: 'assistant',
            content: 'irrelevant answer',
            status: 'ok',
            createdAt: DateTime.utc(2026, 6, 1, 10, 0, 0, 1),
          ),
        ];

    test('editLastTurn 串行调 rollback(1) + send(新内容)，history 重新生成',
        () async {
      final messages = FakeMessagesApi(history: initialPair());
      final streamCtrl = StreamController<ChatEvent>();
      messages.useLiveStream(streamCtrl);
      final ckpt = FakeCheckpointApi(
        rollbackResponse: const RollbackResponse(
            deletedMessages: 2, headCheckpointId: 'h'),
      );
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);

      expect(c.read(chatControllerProvider(sid).notifier).isLastTurnEditable(),
          isTrue);

      // rollback 之后 PG 已没有那一轮
      messages.history = [];

      final fut = c
          .read(chatControllerProvider(sid).notifier)
          .editLastTurn('better question');

      // editLastTurn 内部会先 rollback 再 send；等到 send 触发 streaming
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.streaming);

      // rollback 已被调过、且 lastN=1（一轮）
      expect(ckpt.rollbackCalls, 1);
      expect(ckpt.lastRollbackLastN, 1);
      // send 把新内容当 user 输入塞到 run.userInput
      expect(c.read(chatControllerProvider(sid)).value!.run.userInput,
          'better question');

      // 把流走完
      streamCtrl.add(
        const RunStartEvent(runId: 'r-edit', sessionId: sid, messageId: 'm-edit'),
      );
      streamCtrl.add(const TokenEvent(delta: 'new answer'));
      streamCtrl.add(const FinalEvent(
        messageId: 'm-edit',
        answer: 'new answer',
        citations: [],
        confidence: 0.9,
      ));
      streamCtrl.add(const EndEvent());
      await streamCtrl.close();
      await fut;

      final after =
          await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
      // history 末尾是新一轮 user/assistant
      expect(after.history.length, 2);
      expect(after.history[0].role, 'user');
      expect(after.history[0].content, 'better question');
      expect(after.history[1].role, 'assistant');
      expect(after.history[1].content, 'new answer');
    });

    test('editLastTurn 在跑中 run 时抛 StateError；rollback / send 都不调',
        () async {
      final messages = FakeMessagesApi(history: initialPair());
      final streamCtrl = StreamController<ChatEvent>();
      messages.useLiveStream(streamCtrl);
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);
      unawaited(c.read(chatControllerProvider(sid).notifier).send('q'));
      streamCtrl
          .add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.streaming);

      await expectLater(
        c
            .read(chatControllerProvider(sid).notifier)
            .editLastTurn('whatever'),
        throwsA(isA<StateError>()),
      );
      expect(ckpt.rollbackCalls, 0);

      streamCtrl.add(const FinalEvent(
        messageId: 'm', answer: 'a', citations: [], confidence: 0,
      ));
      streamCtrl.add(const EndEvent());
      await streamCtrl.close();
      await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
    });

    test('history 末尾不是 user+assistant 时 isLastTurnEditable=false 且抛 StateError',
        () async {
      // 末尾是 user，没有 assistant 回复（"上一轮中途中断"）
      final messages = FakeMessagesApi(history: [
        MessageOut(
          id: 'u-only',
          sessionId: sid,
          role: 'user',
          content: 'q',
          status: 'ok',
          createdAt: DateTime.utc(2026, 6, 1),
        ),
      ]);
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);

      expect(c.read(chatControllerProvider(sid).notifier).isLastTurnEditable(),
          isFalse);

      await expectLater(
        c.read(chatControllerProvider(sid).notifier).editLastTurn('x'),
        throwsA(isA<StateError>()),
      );
      expect(ckpt.rollbackCalls, 0);
    });

    test('rollback 失败 → editLastTurn 抛错；send 不被调用，history 保留',
        () async {
      final messages = FakeMessagesApi(history: initialPair());
      final ckpt = FakeCheckpointApi()..failNextOp = 'rollback';
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      await c.read(chatControllerProvider(sid).future);

      await expectLater(
        c
            .read(chatControllerProvider(sid).notifier)
            .editLastTurn('better question'),
        throwsA(isA<CheckpointFakeError>()),
      );
      expect(ckpt.rollbackCalls, 1);
      // history 没动
      final s = c.read(chatControllerProvider(sid)).value!;
      expect(s.history.length, 2);
      expect(s.run.status, RunStatus.idle);
    });
  });

  group('build(): 过滤 paused session 的空 stub assistant', () {
    test('history 中 role=assistant, status=ok, content="" 的 stub 被过滤掉',
        () async {
      final messages = FakeMessagesApi(history: [
        MessageOut(
          id: 'u', sessionId: sid, role: 'user', content: 'q', status: 'ok',
          createdAt: DateTime.utc(2026, 5, 24, 20),
        ),
        MessageOut(
          id: 'a-stub', sessionId: sid, role: 'assistant', content: '',
          status: 'ok', createdAt: DateTime.utc(2026, 5, 24, 20, 1),
        ),
        MessageOut(
          id: 'a-real', sessionId: sid, role: 'assistant',
          content: 'real answer', status: 'ok',
          createdAt: DateTime.utc(2026, 5, 24, 20, 2),
        ),
      ]);
      final ckpt = FakeCheckpointApi();
      final c = _container(messages: messages, checkpoint: ckpt);
      _keepAlive(c, sid);
      final s = await c.read(chatControllerProvider(sid).future);
      expect(s.history.length, 2);
      expect(s.history.map((m) => m.id).toList(), ['u', 'a-real']);
    });
  });
}
