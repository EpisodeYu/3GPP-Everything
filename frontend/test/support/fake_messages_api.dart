import 'dart:async';

import 'package:dio/dio.dart';
import 'package:tgpp/data/api/messages_api.dart';

/// In-memory MessagesApi。可注入：
/// - 一段 SSE 事件序列（按顺序 emit）
/// - 可选的事件间延迟（让 widget 测试观察 streaming 中间态）
/// - `cancelRun` 的回调，让单测捕获 sid/runId
class FakeMessagesApi implements MessagesApi {
  FakeMessagesApi({
    this.events = const [],
    this.delay = Duration.zero,
    this.history = const [],
    this.onSend,
    this.onCancelRun,
  });

  /// 默认要 emit 的事件序列。若 [onSend] 不为 null，[onSend] 接管。
  final List<ChatEvent> events;
  final Duration delay;

  /// 测试可在 build 之后修改它来模拟 PG 状态变化（如 resume / rollback 后的 refetch）。
  List<MessageOut> history;

  /// 让单测拿到 send 参数 + 决定 emit 哪些事件。
  Stream<ChatEvent> Function(String sid, SendMessageBody body)? onSend;
  Future<void> Function(String sid, String runId)? onCancelRun;

  /// 单测自由读：上一次取消的 (sid, runId)。
  String? lastCancelledSid;
  String? lastCancelledRunId;

  /// `list`（GET messages）被调用次数，用于验证草稿会话跳过历史拉取。
  int listCalls = 0;

  /// 给 widget / integration 测试用的 controller：测试自己 push 事件，控制时序。
  StreamController<ChatEvent>? _liveController;

  /// 切到 live 模式：sendMessage 走单测拿到的 controller，不再读 [events]。
  /// 测试自己 `.add(...)` / `.close()`。
  void useLiveStream(StreamController<ChatEvent> controller) {
    _liveController = controller;
  }

  @override
  Future<MessageListResponse> list(
    String sid, {
    int page = 1,
    int pageSize = 200,
  }) async {
    listCalls += 1;
    return MessageListResponse(items: history, total: history.length);
  }

  @override
  Stream<ChatEvent> sendMessage(
    String sid,
    SendMessageBody body, {
    CancelToken? cancelToken,
  }) async* {
    if (_liveController != null) {
      yield* _liveController!.stream;
      return;
    }
    if (onSend != null) {
      yield* onSend!(sid, body);
      return;
    }
    for (final e in events) {
      if (delay > Duration.zero) {
        await Future<void>.delayed(delay);
      }
      yield e;
      if (cancelToken?.isCancelled ?? false) return;
    }
  }

  @override
  Future<void> cancelRun(String sid, String runId) async {
    lastCancelledSid = sid;
    lastCancelledRunId = runId;
    if (onCancelRun != null) await onCancelRun!(sid, runId);
  }
}
