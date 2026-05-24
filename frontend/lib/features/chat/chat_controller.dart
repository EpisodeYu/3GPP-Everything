import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/messages_api.dart';

/// 一轮对话流的当前 run 状态。
///
/// 用 enum 描述 SSE 流式生命周期，状态机锚点：
/// `docs/03-development/05-frontend.md §5.1`。
enum RunStatus {
  /// 还没发过 / 上一轮已彻底收尾，UI 处于可输入态。
  idle,

  /// SSE 流跑起来了；token 持续到来。
  streaming,

  /// 用户点了取消，等后端 cancelled / error / end 收尾。
  cancelling,

  /// `final` event 到了，本轮成功。
  done,

  /// `cancelled` event 到了或者 cancel HTTP 已 204，但流以非错误方式终止。
  cancelled,

  /// `error` event 到了，或前端 dio/解析报错。
  error,
}

/// 单个 Agent 节点的运行状态。M5.2 NodeStatusStrip 渲染的最小单元。
class NodeRunStatus {
  const NodeRunStatus({
    required this.node,
    required this.running,
    this.durationMs,
    this.summary = const {},
  });

  final String node;
  final bool running;
  final int? durationMs;
  final Map<String, dynamic> summary;

  NodeRunStatus copyWith({bool? running, int? durationMs, Map<String, dynamic>? summary}) =>
      NodeRunStatus(
        node: node,
        running: running ?? this.running,
        durationMs: durationMs ?? this.durationMs,
        summary: summary ?? this.summary,
      );
}

/// 一次 send → final/cancel/error 的全部状态。
class ChatRunState {
  const ChatRunState({
    required this.status,
    this.runId,
    this.messageId,
    this.userInput = '',
    this.nodes = const [],
    this.chunksHit = const [],
    this.chunksRerank = const [],
    this.partialAnswer = '',
    this.finalAnswer,
    this.citations = const [],
    this.confidence,
    this.errorMessage,
  });

  const ChatRunState.idle() : this(status: RunStatus.idle);

  final RunStatus status;
  final String? runId;
  final String? messageId;
  final String userInput;
  final List<NodeRunStatus> nodes;
  final List<ChunkPreview> chunksHit;
  final List<ChunkPreview> chunksRerank;
  final String partialAnswer;
  final String? finalAnswer;
  final List<Citation> citations;
  final double? confidence;
  final String? errorMessage;

  bool get isRunning => status == RunStatus.streaming || status == RunStatus.cancelling;

  /// 优先显示 rerank（覆盖 hit 的 loading 占位），fallback 到 hit。
  List<ChunkPreview> get displayedChunks =>
      chunksRerank.isNotEmpty ? chunksRerank : chunksHit;

  ChatRunState copyWith({
    RunStatus? status,
    String? runId,
    String? messageId,
    String? userInput,
    List<NodeRunStatus>? nodes,
    List<ChunkPreview>? chunksHit,
    List<ChunkPreview>? chunksRerank,
    String? partialAnswer,
    String? finalAnswer,
    List<Citation>? citations,
    double? confidence,
    String? errorMessage,
  }) =>
      ChatRunState(
        status: status ?? this.status,
        runId: runId ?? this.runId,
        messageId: messageId ?? this.messageId,
        userInput: userInput ?? this.userInput,
        nodes: nodes ?? this.nodes,
        chunksHit: chunksHit ?? this.chunksHit,
        chunksRerank: chunksRerank ?? this.chunksRerank,
        partialAnswer: partialAnswer ?? this.partialAnswer,
        finalAnswer: finalAnswer ?? this.finalAnswer,
        citations: citations ?? this.citations,
        confidence: confidence ?? this.confidence,
        errorMessage: errorMessage ?? this.errorMessage,
      );
}

/// 整个会话页面的状态：历史消息列表 + 当前 run 状态。
class SessionChatState {
  const SessionChatState({
    required this.history,
    required this.run,
  });

  const SessionChatState.empty()
      : history = const [],
        run = const ChatRunState.idle();

  /// 已落 PG 的消息（user / assistant）按 created_at 升序。
  final List<MessageOut> history;

  /// 当前正在跑的 / 最近完成的 run；done/cancelled/error 时仍保留供 UI 展示。
  final ChatRunState run;

  SessionChatState copyWith({
    List<MessageOut>? history,
    ChatRunState? run,
  }) =>
      SessionChatState(
        history: history ?? this.history,
        run: run ?? this.run,
      );
}

/// 会话级 Riverpod controller。一个会话一份状态；切会话时 autoDispose 析构。
class ChatController extends AutoDisposeFamilyAsyncNotifier<SessionChatState, String> {
  StreamSubscription<ChatEvent>? _sub;
  CancelToken? _cancelToken;

  @override
  Future<SessionChatState> build(String sid) async {
    ref.onDispose(() {
      _sub?.cancel();
      _cancelToken?.cancel('controller_dispose');
    });
    final api = ref.read(messagesApiProvider);
    try {
      final resp = await api.list(sid);
      return SessionChatState(history: resp.items, run: const ChatRunState.idle());
    } on Object {
      // 新会话第一次进来或 API 偶发失败：用空 history 启动，让用户能直接发问；
      // 错误向上抛会让 AsyncNotifier 落 error 态，UI 反而看不到 composer。
      return const SessionChatState.empty();
    }
  }

  /// 发一条消息并启动 SSE 流。再次调用前需要先等上一次结束（streaming/cancelling 状态下 no-op）。
  Future<void> send(
    String content, {
    String? mode,
    List<String> explicitTools = const [],
  }) async {
    final current = state.value;
    if (current == null) return;
    if (current.run.isRunning) return;

    final api = ref.read(messagesApiProvider);
    final cancelToken = CancelToken();
    _cancelToken = cancelToken;

    state = AsyncData(
      current.copyWith(
        run: ChatRunState(
          status: RunStatus.streaming,
          userInput: content,
        ),
      ),
    );

    final body = SendMessageBody(
      content: content,
      mode: mode,
      explicitTools: explicitTools,
    );

    final completer = Completer<void>();
    _sub = api.sendMessage(arg, body, cancelToken: cancelToken).listen(
      _onEvent,
      onError: (Object e, StackTrace st) {
        _markError(e.toString());
        if (!completer.isCompleted) completer.complete();
      },
      onDone: () {
        _onStreamDone();
        if (!completer.isCompleted) completer.complete();
      },
      cancelOnError: true,
    );

    await completer.future;
  }

  /// 取消正在跑的 run。先发 DELETE /runs/{rid}，再 cancel dio token 兜底；
  /// 真正终止由后端 SSE 流的 `cancelled` event 触发，或者流断开。
  Future<void> cancel() async {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    if (!run.isRunning) return;

    state = AsyncData(current.copyWith(run: run.copyWith(status: RunStatus.cancelling)));

    final runId = run.runId;
    if (runId != null) {
      try {
        await ref.read(messagesApiProvider).cancelRun(arg, runId);
      } on Object {
        // 静默；DELETE 失败也要继续兜底 cancel token
      }
    }
    _cancelToken?.cancel('user_cancel');
  }

  void _onEvent(ChatEvent evt) {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    switch (evt) {
      case RunStartEvent():
        state = AsyncData(current.copyWith(
          run: run.copyWith(
            runId: evt.runId,
            messageId: evt.messageId,
          ),
        ));
        break;
      case NodeStartEvent():
        final nodes = [
          ...run.nodes.where((n) => n.node != evt.node),
          NodeRunStatus(node: evt.node, running: true),
        ];
        state = AsyncData(current.copyWith(run: run.copyWith(nodes: nodes)));
        break;
      case NodeEndEvent():
        final nodes = [
          for (final n in run.nodes)
            if (n.node == evt.node)
              n.copyWith(running: false, durationMs: evt.durationMs, summary: evt.summary)
            else
              n,
        ];
        state = AsyncData(current.copyWith(run: run.copyWith(nodes: nodes)));
        break;
      case ChunksHitEvent():
        state = AsyncData(current.copyWith(run: run.copyWith(chunksHit: evt.chunks)));
        break;
      case ChunksRerankEvent():
        state = AsyncData(current.copyWith(run: run.copyWith(chunksRerank: evt.chunks)));
        break;
      case TokenEvent():
        state = AsyncData(current.copyWith(
          run: run.copyWith(partialAnswer: run.partialAnswer + evt.delta),
        ));
        break;
      case FinalEvent():
        state = AsyncData(current.copyWith(
          run: run.copyWith(
            messageId: evt.messageId,
            status: RunStatus.done,
            finalAnswer: evt.answer,
            partialAnswer: evt.answer,
            citations: evt.citations,
            confidence: evt.confidence,
          ),
        ));
        break;
      case CancelledEvent():
        state = AsyncData(current.copyWith(
          run: run.copyWith(status: RunStatus.cancelled, errorMessage: evt.reason),
        ));
        break;
      case ErrorEvent():
        _markError('${evt.code}: ${evt.message}');
        break;
      case EndEvent():
        // 收尾事件；状态机已经在 final/cancelled/error 上落终态了，end 仅用于把已完成的 turn
        // 推入 history 让 composer 重新可用。
        _flushDoneToHistory();
        break;
      case UnknownChatEvent():
        // 未知 event 名（后端日后扩展）—— 忽略
        break;
    }
  }

  void _onStreamDone() {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    // 已经在 _onEvent 里走到终态的（done / cancelled / error），_flushDoneToHistory 已经处理
    if (run.status == RunStatus.streaming || run.status == RunStatus.cancelling) {
      // 流意外断了但没收到 final / cancelled / error
      _markError(run.status == RunStatus.cancelling ? 'cancelled_no_event' : 'stream_closed');
    } else {
      _flushDoneToHistory();
    }
  }

  void _markError(String message) {
    final current = state.value;
    if (current == null) return;
    state = AsyncData(current.copyWith(
      run: current.run.copyWith(status: RunStatus.error, errorMessage: message),
    ));
  }

  /// final/cancelled/error + end 后把这一轮固化为两条 MessageOut（user + assistant）
  /// 推进 history；run 复位到 idle。
  ///
  /// 后端在 final 时已经 UPDATE 了 assistant message 的 content；这里前端不重新拉 history，
  /// 而是用收到的 SSE 数据本地拼一份等价的 MessageOut，省一次 round-trip。
  void _flushDoneToHistory() {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    // 错误态不固化到 history：history 只反映"成功完成 / 被用户主动取消"的 turn；
    // 错误让 errorMessage 留在 run 上让 UI 提示并允许重发。
    if (run.status != RunStatus.done && run.status != RunStatus.cancelled) {
      return;
    }
    if (run.userInput.isEmpty) {
      // 没在前端 send 过（极端 corner case），不动 history
      state = AsyncData(current.copyWith(run: const ChatRunState.idle()));
      return;
    }
    final now = DateTime.now().toUtc();
    final userMsg = MessageOut(
      id: 'local-user-${now.microsecondsSinceEpoch}',
      sessionId: arg,
      role: 'user',
      content: run.userInput,
      status: 'ok',
      createdAt: now,
    );
    final assistant = MessageOut(
      id: run.messageId ?? 'local-asst-${now.microsecondsSinceEpoch}',
      sessionId: arg,
      role: 'assistant',
      content: run.finalAnswer ?? run.partialAnswer,
      status: switch (run.status) {
        RunStatus.done => 'ok',
        RunStatus.cancelled => 'cancelled',
        _ => 'failed',
      },
      createdAt: now,
      confidence: run.confidence,
      citations: [
        for (var i = 0; i < run.citations.length; i++)
          MessageCitationOut(
            chunkId: run.citations[i].chunkId,
            rank: i,
            specId: run.citations[i].specId,
            sectionPath: run.citations[i].sectionPath,
            rerankScore: run.citations[i].rerankScore,
          ),
      ],
    );
    state = AsyncData(SessionChatState(
      history: [...current.history, userMsg, assistant],
      run: const ChatRunState.idle(),
    ));
  }
}

final chatControllerProvider = AutoDisposeAsyncNotifierProvider.family<
    ChatController, SessionChatState, String>(ChatController.new);
