import 'dart:async';

import 'package:dio/dio.dart';
import 'package:tgpp/data/api/checkpoint_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/data/api/sessions_api.dart';

/// 内存版 CheckpointApi。测试通过注入 [resumeEvents] / [forkResponse] /
/// [rollbackResponse] / [checkpoints] 控制各路由返回值；
/// 调用次数 + 参数记录到 `last*` 字段。
class FakeCheckpointApi implements CheckpointApi {
  FakeCheckpointApi({
    this.checkpoints = const <CheckpointOut>[],
    this.forkResponse,
    this.rollbackResponse,
    this.resumeEvents = const <ChatEvent>[],
    this.pauseDelay = Duration.zero,
  });

  /// `list()` 返回的 checkpoints。
  List<CheckpointOut> checkpoints;

  /// `fork()` 返回的新会话。null → 自动构造。
  SessionOut? forkResponse;
  RollbackResponse? rollbackResponse;

  /// `resume()` 流要 emit 的事件序列。
  final List<ChatEvent> resumeEvents;
  final Duration pauseDelay;

  /// resume 期间走 live controller（与 fake_messages_api 同款）。
  StreamController<ChatEvent>? _liveController;
  void useLiveStream(StreamController<ChatEvent> controller) {
    _liveController = controller;
  }

  int pauseCalls = 0;
  int resumeCalls = 0;
  int forkCalls = 0;
  int rollbackCalls = 0;
  int listCalls = 0;
  String? lastPauseSid;
  String? lastPauseRunId;
  String? lastForkSid;
  String? lastForkCheckpointId;
  String? lastForkNewUserMessage;
  String? lastForkTitle;
  String? lastForkUpToMessageId;
  String? lastRollbackSid;
  int? lastRollbackLastN;

  /// 任一方法在 fail 模式下抛 [CheckpointFakeError]；下一次调用自动复位。
  String? failNextOp;

  void _maybeThrow(String op) {
    if (failNextOp == op) {
      failNextOp = null;
      throw CheckpointFakeError(op);
    }
  }

  @override
  Future<PauseResponse> pause(String sid, String runId) async {
    pauseCalls += 1;
    lastPauseSid = sid;
    lastPauseRunId = runId;
    _maybeThrow('pause');
    if (pauseDelay > Duration.zero) {
      await Future<void>.delayed(pauseDelay);
    }
    return PauseResponse(
      runId: runId,
      sessionId: sid,
      status: 'paused',
    );
  }

  @override
  Stream<ChatEvent> resume(String sid, {CancelToken? cancelToken}) async* {
    resumeCalls += 1;
    _maybeThrow('resume');
    if (_liveController != null) {
      yield* _liveController!.stream;
      return;
    }
    for (final e in resumeEvents) {
      yield e;
      if (cancelToken?.isCancelled ?? false) return;
    }
  }

  @override
  Future<CheckpointListResponse> list(String sid) async {
    listCalls += 1;
    _maybeThrow('list');
    return CheckpointListResponse(items: List.of(checkpoints));
  }

  @override
  Future<ForkResponse> fork(
    String sid, {
    required String checkpointId,
    String? newUserMessage,
    String? title,
    String? upToMessageId,
  }) async {
    forkCalls += 1;
    lastForkSid = sid;
    lastForkCheckpointId = checkpointId;
    lastForkNewUserMessage = newUserMessage;
    lastForkTitle = title;
    lastForkUpToMessageId = upToMessageId;
    _maybeThrow('fork');
    final session = forkResponse ?? _autoForkedSession(sid, checkpointId);
    return ForkResponse(newSession: session);
  }

  @override
  Future<RollbackResponse> rollback(String sid, {required int lastN}) async {
    rollbackCalls += 1;
    lastRollbackSid = sid;
    lastRollbackLastN = lastN;
    _maybeThrow('rollback');
    return rollbackResponse ??
        RollbackResponse(
          deletedMessages: lastN,
          headCheckpointId: 'cp-head-after-rollback',
        );
  }

  SessionOut _autoForkedSession(String fromSid, String checkpointId) {
    final now = DateTime.utc(2026, 5, 24, 21);
    return SessionOut(
      id: 'fork-of-$fromSid',
      userId: 'user-1',
      title: 'forked',
      modeDefault: 'qa',
      status: 'active',
      forkedFromSessionId: fromSid,
      forkedFromCheckpointId: checkpointId,
      createdAt: now,
      updatedAt: now,
    );
  }
}

class CheckpointFakeError implements Exception {
  CheckpointFakeError(this.op);
  final String op;
  @override
  String toString() => 'CheckpointFakeError($op)';
}

CheckpointOut buildCheckpoint({
  String checkpointId = 'cp-1',
  String? parentCheckpointId,
  String? lastNode,
  List<String> nextNodes = const [],
  String createdAt = '2026-05-24T20:00:00Z',
}) =>
    CheckpointOut(
      checkpointId: checkpointId,
      parentCheckpointId: parentCheckpointId,
      lastNode: lastNode,
      nextNodes: nextNodes,
      createdAt: createdAt,
    );
