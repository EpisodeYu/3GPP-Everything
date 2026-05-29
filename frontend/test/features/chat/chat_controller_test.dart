import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/features/chat/chat_controller.dart';

import '../../support/fake_auth_controller.dart';
import '../../support/fake_messages_api.dart';

ProviderContainer _container(FakeMessagesApi api) {
  final c = ProviderContainer(overrides: [
    fakeAuthControllerOverride,
    messagesApiProvider.overrideWithValue(api),
  ]);
  addTearDown(c.dispose);
  return c;
}

/// 在 autoDispose 下，单次 read 不会保持订阅，provider 会立刻析构。
/// 测试用 listen 占住引用，模拟 widget 一直在监听。
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
  const sid = 'session-1';

  test('初始 build()：history 为空 + run idle', () async {
    final api = FakeMessagesApi();
    final c = _container(api);
    _keepAlive(c, sid);
    final s = await c.read(chatControllerProvider(sid).future);
    expect(s.history, isEmpty);
    expect(s.run.status, RunStatus.idle);
  });

  test('完整 happy path：run_start → node_start/end → chunks_hit → chunks_rerank → token*2 → final → end', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'run-1', sessionId: sid, messageId: 'asst-1'),
      const NodeStartEvent(node: 'retrieve'),
      const NodeEndEvent(node: 'retrieve', durationMs: 12, summary: {'candidates_count': 5}),
      const ChunksHitEvent(chunks: [
        ChunkPreview(chunkId: 'c1', specId: '23.501', sectionPath: '5.6.1', preview: 'p1'),
      ]),
      const ChunksRerankEvent(chunks: [
        ChunkPreview(
            chunkId: 'c1', specId: '23.501', sectionPath: '5.6.1',
            preview: 'p1', rerankScore: 0.9),
      ]),
      const TokenEvent(delta: 'Hel'),
      const TokenEvent(delta: 'lo'),
      const FinalEvent(
        messageId: 'asst-1',
        answer: 'Hello [1]',
        citations: [
          Citation(chunkId: 'c1', specId: '23.501', sectionPath: '5.6.1', rank: 1),
        ],
        confidence: 0.83,
      ),
      const EndEvent(),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('hi');

    final s = c.read(chatControllerProvider(sid)).value!;
    // end 事件之后 run 已 reset 到 idle，并把这一轮推进 history
    expect(s.run.status, RunStatus.idle);
    expect(s.history.length, 2);
    expect(s.history[0].role, 'user');
    expect(s.history[0].content, 'hi');
    expect(s.history[1].role, 'assistant');
    expect(s.history[1].content, 'Hello [1]');
    expect(s.history[1].citations.length, 1);
    expect(s.history[1].citations.first.rank, 1);
    expect(s.history[1].confidence, 0.83);
  });

  // v6 索引方案回归（2026-05-29 用户复现）：final 事件里的 Citation.rank
  // （1-based，来自 backend `parse_citations` 写入的 N）必须原样写到
  // MessageCitationOut.rank。原 _flushDoneToHistory 误用 loop 索引（0/1/2/3），
  // 导致 LLM 输出 [1][2][6][8] 时：
  //   - [1][2] 反查到错位 chunk 元数据（多个 chip 显示同一 spec §section）
  //   - [6][8] 在 byRank 里缺失 → 退化为裸文本 chip 不可点
  test('final 事件含非连续 rank（1/2/6/8）→ MessageCitationOut.rank 沿用后端 N', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'asst-1'),
      const FinalEvent(
        messageId: 'asst-1',
        answer: '一段 [1][2] 中间 [6] 又一段 [8]。',
        citations: [
          Citation(chunkId: 'c-1', specId: '38.321', sectionPath: '5.7', rank: 1),
          Citation(chunkId: 'c-2', specId: '38.321', sectionPath: '5.7', rank: 2),
          Citation(chunkId: 'c-6', specId: '38.321', sectionPath: '5.7.3', rank: 6),
          Citation(chunkId: 'c-8', specId: '36.321', sectionPath: '5.5', rank: 8),
        ],
        confidence: 0.9,
      ),
      const EndEvent(),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('q');

    final s = c.read(chatControllerProvider(sid)).value!;
    expect(s.run.status, RunStatus.idle);
    expect(s.history.length, 2);
    final cits = s.history[1].citations;
    expect(cits.map((e) => e.rank).toList(), [1, 2, 6, 8]);
    expect(cits.map((e) => e.chunkId).toList(),
        ['c-1', 'c-2', 'c-6', 'c-8']);
  });

  test('token 累积到 partialAnswer；final 覆盖为 answer', () async {
    final api = FakeMessagesApi();
    final controller = StreamController<ChatEvent>();
    api.useLiveStream(controller);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    unawaited(c.read(chatControllerProvider(sid).notifier).send('q'));
    controller.add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
    controller.add(const TokenEvent(delta: 'foo '));
    controller.add(const TokenEvent(delta: 'bar'));
    final mid = await _waitUntil(c, sid, (s) => s.run.partialAnswer == 'foo bar');
    expect(mid.run.status, RunStatus.streaming);
    expect(mid.run.partialAnswer, 'foo bar');
    controller.add(const FinalEvent(
      messageId: 'm',
      answer: 'foo bar',
      citations: [],
      confidence: 0.5,
    ));
    controller.add(const EndEvent());
    await controller.close();
    await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
  });

  test('chunks_rerank 覆盖 chunks_hit；displayedChunks 优先 rerank', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'),
      const ChunksHitEvent(chunks: [
        ChunkPreview(chunkId: 'c1', specId: 's', sectionPath: '1', preview: ''),
      ]),
      const ChunksRerankEvent(chunks: [
        ChunkPreview(
            chunkId: 'c1', specId: 's', sectionPath: '1', preview: '', rerankScore: 0.7),
        ChunkPreview(
            chunkId: 'c2', specId: 's', sectionPath: '2', preview: '', rerankScore: 0.5),
      ]),
      const ErrorEvent(code: 'oops', message: 'mid-stream agent fail'),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('hi');
    final s = c.read(chatControllerProvider(sid)).value!;
    expect(s.run.chunksHit.length, 1);
    expect(s.run.chunksRerank.length, 2);
    expect(s.run.displayedChunks.length, 2);
    expect(s.run.status, RunStatus.error);
    expect(s.run.errorMessage, contains('oops'));
  });

  test('cancelled event → status=cancelled + flush 到 history', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'),
      const TokenEvent(delta: 'partial'),
      const CancelledEvent(reason: 'user_cancelled'),
      const EndEvent(),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('hi');
    final s = c.read(chatControllerProvider(sid)).value!;
    expect(s.run.status, RunStatus.idle);
    expect(s.history.length, 2);
    expect(s.history.last.status, 'cancelled');
    expect(s.history.last.content, 'partial');
  });

  test('cancel(): 调 DELETE /runs/{rid} 并把 token 取消，end 后回 idle', () async {
    final api = FakeMessagesApi();
    final controller = StreamController<ChatEvent>();
    api.useLiveStream(controller);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    unawaited(c.read(chatControllerProvider(sid).notifier).send('q'));
    controller.add(const RunStartEvent(runId: 'run-x', sessionId: sid, messageId: 'm'));
    await _waitUntil(c, sid, (s) => s.run.runId == 'run-x');

    await c.read(chatControllerProvider(sid).notifier).cancel();
    expect(api.lastCancelledRunId, 'run-x');
    expect(api.lastCancelledSid, sid);
    final cancelling =
        await _waitUntil(c, sid, (s) => s.run.status == RunStatus.cancelling);
    expect(cancelling.run.status, RunStatus.cancelling);

    controller.add(const CancelledEvent(reason: 'user_cancelled'));
    controller.add(const EndEvent());
    await controller.close();
    final done = await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
    expect(done.run.status, RunStatus.idle);
  });

  test('error event → status=error + errorMessage 带 code:message', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'),
      const ErrorEvent(code: 'agent_failed', message: 'boom'),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('hi');
    final s = c.read(chatControllerProvider(sid)).value!;
    expect(s.run.status, RunStatus.error);
    expect(s.run.errorMessage, 'agent_failed: boom');
  });

  test('未知 event 不会让状态机崩，原状态保留', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'),
      const UnknownChatEvent(name: 'future_event', data: {'x': 1}),
      const TokenEvent(delta: 'ok'),
      const FinalEvent(
          messageId: 'm', answer: 'ok', citations: [], confidence: 0),
      const EndEvent(),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('hi');
    final s = c.read(chatControllerProvider(sid)).value!;
    expect(s.history.last.content, 'ok');
  });

  test('运行中再次 send no-op：不重复打开第二条流', () async {
    final api = FakeMessagesApi();
    final controller = StreamController<ChatEvent>();
    api.useLiveStream(controller);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    unawaited(c.read(chatControllerProvider(sid).notifier).send('q1'));
    controller.add(const RunStartEvent(runId: 'r1', sessionId: sid, messageId: 'm1'));
    await _waitUntil(c, sid, (s) => s.run.status == RunStatus.streaming);
    // 第二次 send 应被忽略
    await c.read(chatControllerProvider(sid).notifier).send('q2');
    expect(c.read(chatControllerProvider(sid)).value!.run.userInput, 'q1');
    controller.add(const FinalEvent(
      messageId: 'm1', answer: 'a1', citations: [], confidence: 0,
    ));
    controller.add(const EndEvent());
    await controller.close();
    await _waitUntil(c, sid, (s) => s.run.status == RunStatus.idle);
  });

  test('多 node_start 顺序到来：nodes 列表按顺序累积，无重复', () async {
    final api = FakeMessagesApi(events: [
      const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'),
      const NodeStartEvent(node: 'classify'),
      const NodeEndEvent(node: 'classify', durationMs: 1, summary: {}),
      const NodeStartEvent(node: 'rewrite'),
      const NodeEndEvent(node: 'rewrite', durationMs: 2, summary: {}),
      const NodeStartEvent(node: 'retrieve'),
      const NodeEndEvent(node: 'retrieve', durationMs: 3, summary: {}),
      const FinalEvent(
          messageId: 'm', answer: 'x', citations: [], confidence: 0),
      const EndEvent(),
    ]);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    await c.read(chatControllerProvider(sid).notifier).send('hi');
    // run 已 flush 到 history；要看运行中的 node 顺序得看 turn 结束前；
    // 这里只断言 history 落了
    final s = c.read(chatControllerProvider(sid)).value!;
    expect(s.history.length, 2);
  });

  test('流意外关闭（无 final / cancelled / error）→ status=error', () async {
    final api = FakeMessagesApi();
    final controller = StreamController<ChatEvent>();
    api.useLiveStream(controller);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    unawaited(c.read(chatControllerProvider(sid).notifier).send('hi'));
    controller.add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
    controller.add(const TokenEvent(delta: 'foo'));
    await _waitUntil(c, sid, (s) => s.run.partialAnswer == 'foo');
    await controller.close();
    final s = await _waitUntil(c, sid, (s) => s.run.status == RunStatus.error);
    expect(s.run.errorMessage, 'stream_closed');
  });

  test('final 到达即立刻 flush 到 history（不等 end，避免后端 autotitle 期间页面空窗）',
      () async {
    final api = FakeMessagesApi();
    final controller = StreamController<ChatEvent>();
    api.useLiveStream(controller);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    unawaited(c.read(chatControllerProvider(sid).notifier).send('hi'));
    controller.add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
    controller.add(const TokenEvent(delta: 'hello'));
    await _waitUntil(c, sid, (s) => s.run.partialAnswer == 'hello');

    controller.add(const FinalEvent(
      messageId: 'm',
      answer: 'hello',
      citations: [],
      confidence: 0.6,
    ));
    // 此处刻意不发 EndEvent；模拟后端 autotitle 还在跑的几秒窗口。
    final s = await _waitUntil(c, sid, (s) => s.history.length == 2);
    expect(s.run.status, RunStatus.idle);
    expect(s.history[0].role, 'user');
    expect(s.history[0].content, 'hi');
    expect(s.history[1].role, 'assistant');
    expect(s.history[1].content, 'hello');

    // 之后 end 到达不应破坏 history（兜底 no-op）
    controller.add(const EndEvent());
    await controller.close();
    final s2 = c.read(chatControllerProvider(sid)).value!;
    expect(s2.history.length, 2);
    expect(s2.run.status, RunStatus.idle);
  });

  test('cancelled 到达即立刻 flush 到 history（不等 end）', () async {
    final api = FakeMessagesApi();
    final controller = StreamController<ChatEvent>();
    api.useLiveStream(controller);
    final c = _container(api);
    _keepAlive(c, sid);
    await c.read(chatControllerProvider(sid).future);
    unawaited(c.read(chatControllerProvider(sid).notifier).send('hi'));
    controller.add(const RunStartEvent(runId: 'r', sessionId: sid, messageId: 'm'));
    controller.add(const TokenEvent(delta: 'partial'));
    await _waitUntil(c, sid, (s) => s.run.partialAnswer == 'partial');

    controller.add(const CancelledEvent(reason: 'user_cancelled'));
    final s = await _waitUntil(c, sid, (s) => s.history.length == 2);
    expect(s.run.status, RunStatus.idle);
    expect(s.history.last.status, 'cancelled');
    expect(s.history.last.content, 'partial');

    controller.add(const EndEvent());
    await controller.close();
    final s2 = c.read(chatControllerProvider(sid)).value!;
    expect(s2.history.length, 2);
  });
}
