import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/checkpoint_api.dart';
import '../../data/api/messages_api.dart';
import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';
import '../../domain/session/sessions_controller.dart';

/// 一轮对话流的当前 run 状态。
///
/// 用 enum 描述 SSE 流式生命周期，状态机锚点：
/// `docs/03-development/05-frontend.md §5.1` + §5.5（M5.4 暂停）。
enum RunStatus {
  /// 还没发过 / 上一轮已彻底收尾，UI 处于可输入态。
  idle,

  /// SSE 流跑起来了；token 持续到来。
  streaming,

  /// 用户点了取消，等后端 cancelled / error / end 收尾。
  cancelling,

  /// 用户点了暂停（M5.4）：保留 checkpoint + run 状态；composer 显示"恢复"按钮。
  /// 后端把 session.status 落到 `paused`，SSE 流会自然 onDone，但不算错误。
  paused,

  /// `final` event 到了，本轮成功。
  done,

  /// `cancelled` event 到了或者 cancel HTTP 已 204，但流以非错误方式终止。
  cancelled,

  /// `error` event 到了，或前端 dio/解析报错。
  error,
}

/// 单个 Agent 节点的运行状态。M5.2 起由 ReasoningPanel 消费（2026-05-31 之前
/// 是独立 NodeStatusStrip）。
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

/// 一轮对话完成后冻结下来的 reasoning 快照（2026-06-01）。
///
/// final/cancelled 把本轮推进 history 后 run 会复位到 idle，原本嵌在 streaming
/// bubble 上方的 reasoning 折叠框会随之消失。用户要求「答案出来后过程框不要完全
/// 隐藏，只收起、还能展开」，故 [ChatController._flushDoneToHistory] 把本轮
/// reasoning 冻结成快照、按 assistant message id 存进
/// [SessionChatState.reasoningByMessageId]，由 [ReasoningPanel]（默认折叠 +
/// frozenElapsed）渲染成可点开复盘的历史折叠框。
///
/// 仅覆盖「本会话视图内刚跑完」的轮次；切会话 / 刷新从 PG 重新加载历史时不带
/// reasoning（后端不持久化节点 summary），这些旧消息不显示折叠框。
class ReasoningSnapshot {
  const ReasoningSnapshot({
    required this.nodes,
    required this.reasoningByNode,
    required this.elapsed,
  });

  final List<NodeRunStatus> nodes;
  final Map<String, String> reasoningByNode;

  /// run_start 到收尾的总耗时；折叠态「已思考 X.Xs」展示这个固定值。
  final Duration elapsed;
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
    this.reasoningByNode = const {},
    this.activeNode,
    this.reasoningStartedAt,
    this.reasoningCollapsed = false,
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

  /// 节点级 reasoning 累积文本。当前只 `hyde` 写入（来自 [NodeProgressEvent]
  /// 的字符级 delta），其它节点的"reasoning 内容"由 [ReasoningPanel] 在 widget
  /// 层从 [NodeRunStatus.summary] + i18n 渲染。
  ///
  /// 设计：
  /// - 后端 hyde 改用 `chat_stream()` 真流式（详见 `docs/03-development/03-agent.md §7`），
  ///   每个 chunk 由 SSE `node_progress` 事件推到前端
  /// - controller 把 delta 追加到 `reasoningByNode['hyde']`，UI 字符级刷新
  /// - 流结束 → `reasoningByNode['hyde']` 是完整 hyde_doc，折叠态点开仍能复盘
  final Map<String, String> reasoningByNode;

  /// 当前 running 节点名；用于 [ReasoningPanel] 决定灰色文字区显示哪一段。
  /// `node_start` 设、`node_end` 清空（若是当前 active）。
  final String? activeNode;

  /// reasoning 折叠框开始计时点：`run_start` 时设，用于折叠态显示「已思考 X.Xs」。
  final DateTime? reasoningStartedAt;

  /// reasoning 框是否折叠。默认 false（展开）；首个 `token` 事件到达后由
  /// controller 切 true，UI 折叠成单行。用户手动展开/折叠由 widget 端 override。
  final bool reasoningCollapsed;

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
    Map<String, String>? reasoningByNode,
    Object? activeNode = _unset,
    Object? reasoningStartedAt = _unset,
    bool? reasoningCollapsed,
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
        reasoningByNode: reasoningByNode ?? this.reasoningByNode,
        activeNode: identical(activeNode, _unset)
            ? this.activeNode
            : activeNode as String?,
        reasoningStartedAt: identical(reasoningStartedAt, _unset)
            ? this.reasoningStartedAt
            : reasoningStartedAt as DateTime?,
        reasoningCollapsed: reasoningCollapsed ?? this.reasoningCollapsed,
      );
}

/// copyWith 的 sentinel：让 `activeNode: null` / `reasoningStartedAt: null` 能
/// 表达"显式清空"，与"参数未传"区分（默认 named param 无法区分二者）。
const Object _unset = Object();

/// 整个会话页面的状态：历史消息列表 + 当前 run 状态。
class SessionChatState {
  const SessionChatState({
    required this.history,
    required this.run,
    this.reasoningByMessageId = const {},
  });

  const SessionChatState.empty()
      : history = const [],
        run = const ChatRunState.idle(),
        reasoningByMessageId = const {};

  /// 已落 PG 的消息（user / assistant）按 created_at 升序。
  final List<MessageOut> history;

  /// 当前正在跑的 / 最近完成的 run；done/cancelled/error 时仍保留供 UI 展示。
  final ChatRunState run;

  /// assistant message id → 本轮 reasoning 快照。让答案完成后过程框仍能展开
  /// 复盘（见 [ReasoningSnapshot]）。从 PG 重新加载历史时为空。
  final Map<String, ReasoningSnapshot> reasoningByMessageId;

  SessionChatState copyWith({
    List<MessageOut>? history,
    ChatRunState? run,
    Map<String, ReasoningSnapshot>? reasoningByMessageId,
  }) =>
      SessionChatState(
        history: history ?? this.history,
        run: run ?? this.run,
        reasoningByMessageId:
            reasoningByMessageId ?? this.reasoningByMessageId,
      );
}

/// 会话级 Riverpod controller。一个会话一份状态；切会话时 autoDispose 析构。
class ChatController extends AutoDisposeFamilyAsyncNotifier<SessionChatState, String> {
  StreamSubscription<ChatEvent>? _sub;
  CancelToken? _cancelToken;

  /// 标记当前流来自 resume：final 时走 PG refetch 路径，避免 stub / 重复消息。
  bool _isResume = false;

  @override
  Future<SessionChatState> build(String sid) async {
    ref.onDispose(() {
      _sub?.cancel();
      _cancelToken?.cancel('controller_dispose');
    });

    // 必须等待鉴权状态恢复。
    final authState = await ref.watch(authControllerProvider.future);
    if (authState is! AuthAuthenticated) {
      return const SessionChatState.empty();
    }

    // 刚 createBlank 出来、还没发过消息的空草稿会话：历史必为空，直接用空状态，
    // 跳过 GET /sessions/{sid}/messages 这次网络往返 —— 点"新会话"→进可输入态省一个 RTT。
    if (ref.read(sessionsControllerProvider.notifier).isDraft(sid)) {
      return const SessionChatState.empty();
    }

    return _loadHistoryFromPg();
  }

  /// 从 PG 拉一次历史消息；过滤 paused session 残留的空 stub assistant
  /// （`role=assistant && status=ok && content=''`，由 send → pause 路径产生）。
  Future<SessionChatState> _loadHistoryFromPg() async {
    final api = ref.read(messagesApiProvider);
    try {
      final resp = await api.list(arg);
      final filtered = resp.items
          .where(
            (m) => !(m.role == 'assistant' &&
                m.status == 'ok' &&
                m.content.isEmpty),
          )
          .toList();
      return SessionChatState(
        history: filtered,
        run: const ChatRunState.idle(),
      );
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
    _isResume = false;

    state = AsyncData(
      current.copyWith(
        run: ChatRunState(
          status: RunStatus.streaming,
          userInput: content,
          reasoningStartedAt: DateTime.now(),
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

  /// 暂停正在跑的 run（M5.4）。保留 run 状态（partialAnswer / nodes / chunksHit）。
  ///
  /// 后端会把 session.status 标 `paused`、写 checkpoint，本地状态机切到
  /// [RunStatus.paused]；onStreamDone 会在 paused 态下静默退出，不再标 error。
  Future<void> pause() async {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    if (run.status != RunStatus.streaming) return;
    final runId = run.runId;
    if (runId == null) return;

    state = AsyncData(current.copyWith(
      run: run.copyWith(status: RunStatus.paused),
    ));
    try {
      await ref.read(checkpointApiProvider).pause(arg, runId);
    } on Object catch (e) {
      // pause API 失败 → 回退到 streaming + 显示错误
      final cur = state.value;
      if (cur == null) return;
      state = AsyncData(cur.copyWith(
        run: cur.run.copyWith(
          status: RunStatus.streaming,
          errorMessage: 'pause_failed: $e',
        ),
      ));
    }
  }

  /// 续跑暂停 / 关浏览器重进的 paused 会话（M5.4）。
  ///
  /// 入口允许 `paused` / `idle` / `error` / `done` / `cancelled` 状态；
  /// 阻止 `streaming` / `cancelling`。final 后从 PG refetch history，
  /// 把后端在 stub assistant 上 UPDATE 的 content 拉回来。
  Future<void> resume() async {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    if (run.status == RunStatus.streaming || run.status == RunStatus.cancelling) {
      return;
    }

    final api = ref.read(checkpointApiProvider);
    final cancelToken = CancelToken();
    _cancelToken = cancelToken;
    _isResume = true;

    state = AsyncData(current.copyWith(
      run: run.copyWith(
        status: RunStatus.streaming,
        // resume 续跑时也重置 reasoning 计时与折叠态：续跑过程仍可能跑到 hyde
        // 之外的剩余节点；首个 token 到达再折叠。
        reasoningStartedAt: DateTime.now(),
        reasoningCollapsed: false,
      ),
    ));

    final completer = Completer<void>();
    _sub = api.resume(arg, cancelToken: cancelToken).listen(
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

  /// 删除会话最后 N 轮 messages + LangGraph checkpoint（M5.4）。
  ///
  /// 调用方式：会话设置菜单"删除最后 N 轮"→ slider → 二次确认 → 此函数。
  /// 当前 run 仍在跑 → 抛 [StateError]，UX 上应先 pause / cancel 再 rollback。
  Future<RollbackResponse> rollback(int lastN) async {
    final current = state.value;
    if (current == null) {
      throw StateError('chat_not_loaded');
    }
    if (current.run.isRunning) {
      throw StateError('rollback_with_inflight_run');
    }
    final api = ref.read(checkpointApiProvider);
    final resp = await api.rollback(arg, lastN: lastN);
    // 后端已 cascade 删了 message_citations；前端从 PG 刷一次保证一致
    state = AsyncData(await _loadHistoryFromPg());
    return resp;
  }

  /// 拉取 checkpoint 列表（fork 之前需要拿一个 checkpoint_id）。M5.4。
  Future<CheckpointListResponse> listCheckpoints() async {
    return ref.read(checkpointApiProvider).list(arg);
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
        state = AsyncData(current.copyWith(
          run: run.copyWith(nodes: nodes, activeNode: evt.node),
        ));
        break;
      case NodeEndEvent():
        final nodes = [
          for (final n in run.nodes)
            if (n.node == evt.node)
              n.copyWith(running: false, durationMs: evt.durationMs, summary: evt.summary)
            else
              n,
        ];
        // 节点收尾：若它正是当前 active，清掉 activeNode（下一个 node_start 会重设）
        final clearActive = run.activeNode == evt.node;
        state = AsyncData(current.copyWith(
          run: run.copyWith(
            nodes: nodes,
            activeNode: clearActive ? null : run.activeNode,
          ),
        ));
        break;
      case NodeProgressEvent():
        // hyde 字符级流：把 delta 累加到 reasoningByNode[node]，UI 字符级渲染。
        // 协议只允许 node 非空；防御性 trim 一下，空 delta 不动。
        if (evt.node.isEmpty || evt.delta.isEmpty) break;
        final updated = Map<String, String>.from(run.reasoningByNode);
        updated[evt.node] = (updated[evt.node] ?? '') + evt.delta;
        state = AsyncData(current.copyWith(
          run: run.copyWith(reasoningByNode: updated),
        ));
        break;
      case ChunksHitEvent():
        state = AsyncData(current.copyWith(run: run.copyWith(chunksHit: evt.chunks)));
        break;
      case ChunksRerankEvent():
        state = AsyncData(current.copyWith(run: run.copyWith(chunksRerank: evt.chunks)));
        break;
      case TokenEvent():
        // 首个 token 到达 → reasoning 折叠成单行；user 手动展开靠 widget 层
        // 维护本地 override（_userOverride），不回到 controller。
        final shouldCollapse = !run.reasoningCollapsed && run.partialAnswer.isEmpty;
        state = AsyncData(current.copyWith(
          run: run.copyWith(
            partialAnswer: run.partialAnswer + evt.delta,
            reasoningCollapsed: shouldCollapse ? true : run.reasoningCollapsed,
          ),
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
        // 立即固化到 history，避免后端 final → end 之间的几秒空窗
        // （autotitle LLM 调用）导致页面上本轮消息暂时"消失"。
        // end 事件仍会触发 _flushDoneToHistory，但 status 已 idle 时为 no-op。
        _flushDoneToHistory();
        break;
      case CancelledEvent():
        state = AsyncData(current.copyWith(
          run: run.copyWith(status: RunStatus.cancelled, errorMessage: evt.reason),
        ));
        _flushDoneToHistory();
        break;
      case ErrorEvent():
        _markError('${evt.code}: ${evt.message}');
        break;
      case TitleEvent():
        // 首轮自动标题：仅刷新 sessions 列表（sidebar / chat header 标题），不动本 run 状态。
        ref
            .read(sessionsControllerProvider.notifier)
            .applyTitle(evt.sessionId, evt.title);
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
    // paused：后端在 pause 后会自然关流，保留 run 状态等 resume，不算错误
    if (run.status == RunStatus.paused) {
      return;
    }
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

  /// final/cancelled/error + end 后把这一轮固化为 history；run 复位到 idle。
  ///
  /// 路径分两类：
  /// 1. send → final / cancelled：本地拼 user + assistant，省一次 round-trip
  /// 2. resume → final（[_isResume]=true）：后端已 UPDATE stub assistant 的 content，
  ///    从 PG refetch history 把它拉回来，避免 stub 重复 / 内容空两个问题
  void _flushDoneToHistory() {
    final current = state.value;
    if (current == null) return;
    final run = current.run;
    // 错误态不固化到 history：history 只反映"成功完成 / 被用户主动取消"的 turn；
    // 错误让 errorMessage 留在 run 上让 UI 提示并允许重发。
    if (run.status != RunStatus.done && run.status != RunStatus.cancelled) {
      return;
    }
    if (_isResume) {
      _isResume = false;
      final snapshot = _snapshotFromRun(run);
      final msgId = run.messageId;
      unawaited(() async {
        try {
          final reloaded = await _loadHistoryFromPg();
          state = AsyncData(
            (snapshot != null && msgId != null)
                ? reloaded.copyWith(reasoningByMessageId: {
                    ...current.reasoningByMessageId,
                    msgId: snapshot,
                  })
                : reloaded,
          );
        } on Object {
          state = AsyncData(current.copyWith(run: const ChatRunState.idle()));
        }
      }());
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
      // v6 索引方案：rank 必须沿用后端 final 事件里的 1-based N（即 LLM 原始
      // `[N]` 中的 N，由 backend `parse_citations` 写入），**禁止用 loop 索引**。
      // 用 loop 索引会让 [6][8] 这类非连续 / 跳号引用在 citationsByRank 里找不到
      // 对应条目 → chip 退化为裸文本；同时 [1][2] 会反查到错位的 chunk 元数据，
      // 表现为多个 chip 显示同一 spec §section（用户 2026-05-29 复现）。
      citations: [
        for (final cit in run.citations)
          MessageCitationOut(
            chunkId: cit.chunkId,
            rank: cit.rank,
            specId: cit.specId,
            sectionPath: cit.sectionPath,
            rerankScore: cit.rerankScore,
          ),
      ],
    );
    final snapshot = _snapshotFromRun(run);
    state = AsyncData(SessionChatState(
      history: [...current.history, userMsg, assistant],
      run: const ChatRunState.idle(),
      reasoningByMessageId: snapshot == null
          ? current.reasoningByMessageId
          : {...current.reasoningByMessageId, assistant.id: snapshot},
    ));
  }

  /// 把本轮 run 的 reasoning 冻结成快照供答案完成后展开复盘（见 [ReasoningSnapshot]）。
  /// 没有任何节点 / hyde 流 → null（不显示历史折叠框）。
  ReasoningSnapshot? _snapshotFromRun(ChatRunState run) {
    if (run.nodes.isEmpty && run.reasoningByNode.isEmpty) return null;
    final started = run.reasoningStartedAt;
    return ReasoningSnapshot(
      nodes: run.nodes,
      reasoningByNode: run.reasoningByNode,
      elapsed:
          started != null ? DateTime.now().difference(started) : Duration.zero,
    );
  }
}

final chatControllerProvider = AutoDisposeAsyncNotifierProvider.family<
    ChatController, SessionChatState, String>(ChatController.new);
