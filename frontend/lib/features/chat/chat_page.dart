import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/l10n/app_localizations.dart';
import '../../data/api/favorites_api.dart';
import '../../data/api/feedback_api.dart';
import '../../data/api/messages_api.dart';
import '../../data/api/notes_api.dart';
import '../../data/api/sessions_api.dart';
import '../../domain/session/sessions_controller.dart';
import '../shell/new_session_button.dart';
import 'chat_controller.dart';
import 'widgets/composer.dart';
import 'widgets/message_bubble.dart';
import 'widgets/reasoning_panel.dart';

/// M5.2 起接通 SSE 流式问答；M5.4 加上 pause/resume/fork/rollback + 长按菜单。
///
/// - 无 sessionId（`/chat`）：欢迎/引导页
/// - 有 sessionId（`/sessions/:sid`）：找到会话 → ChatView；找不到 → 提示返回
class ChatPage extends ConsumerWidget {
  const ChatPage({super.key, this.sessionId});

  final String? sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (sessionId == null) {
      return const _WelcomePane();
    }
    final sessionsAsync = ref.watch(sessionsControllerProvider);
    return sessionsAsync.when(
      data: (items) {
        final s = items.where((x) => x.id == sessionId).firstOrNull;
        if (s == null) {
          return const _MissingSessionPane();
        }
        // key 绑 session.id：切换会话时让旧 _ChatViewState 析构（dispose），
        // 从而触发"离开空草稿会话即丢弃"（Req2）。
        return _ChatView(key: ValueKey('chatview-${s.id}'), session: s);
      },
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Center(child: Text('会话加载失败：$e')),
    );
  }
}

class _WelcomePane extends ConsumerWidget {
  const _WelcomePane();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 480),
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.center,
            children: [
              Icon(
                Icons.chat_bubble_outline,
                size: 56,
                color: Theme.of(context).colorScheme.primary,
              ),
              const SizedBox(height: 16),
              Text(
                AppLocalizations.of(context).chatEmptyTitle,
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 24),
              const NewSessionButton(buttonKey: Key('welcome_new_session')),
            ],
          ),
        ),
      ),
    );
  }
}

class _MissingSessionPane extends StatelessWidget {
  const _MissingSessionPane();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.search_off, size: 48),
          const SizedBox(height: 12),
          Text(
            '找不到该会话',
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 12),
          OutlinedButton(
            key: const Key('missing_back_home'),
            onPressed: () => context.go('/chat'),
            child: const Text('返回首页'),
          ),
        ],
      ),
    );
  }
}

class _ChatView extends ConsumerStatefulWidget {
  const _ChatView({super.key, required this.session});
  final SessionOut session;

  @override
  ConsumerState<_ChatView> createState() => _ChatViewState();
}

class _ChatViewState extends ConsumerState<_ChatView> {
  final ScrollController _scroll = ScrollController();

  /// 在 initState 捕获 long-lived notifier：dispose 里 `ref` 已失效不能再 read。
  late final SessionsController _sessions;

  /// 修改最后一次提问（2026-06-02 改 inline 编辑）：非 null → 这条 user 气泡
  /// 进入 inline 编辑态（气泡变 TextField + 右下角发送/取消图标）。底部 composer
  /// 不再切到编辑模式，保持单一职责。发送或取消后回到 null。
  String? _editingMessageId;

  @override
  void initState() {
    super.initState();
    _sessions = ref.read(sessionsControllerProvider.notifier);
  }

  @override
  void dispose() {
    // Req2：离开这个会话时，若它仍是没发过消息的空草稿，丢弃它（不留空会话）。
    // 微任务延后执行，避免在 widget 析构途中改动被 sidebar 监听的 provider。
    final sid = widget.session.id;
    Future.microtask(() => _sessions.discardDraft(sid));
    _scroll.dispose();
    super.dispose();
  }

  void _scrollToBottom() {
    if (!_scroll.hasClients) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
      );
    });
  }

  String get _sid => widget.session.id;

  bool _shouldShowPausedBanner(SessionChatState? s) {
    final runStatus = s?.run.status;
    if (runStatus == RunStatus.paused) return true;
    // 用户关浏览器后重进 paused 会话：controller 处于 idle，session.status 仍是 paused
    if (runStatus == RunStatus.idle && widget.session.status == 'paused') {
      return true;
    }
    return false;
  }

  Future<void> _onPause() async {
    await ref.read(chatControllerProvider(_sid).notifier).pause();
  }

  Future<void> _onResume() async {
    await ref.read(chatControllerProvider(_sid).notifier).resume();
    if (!mounted) return;
    // resume 完成后后端把 session.status 拉回 active；同步 sessions list 让
    // banner 消失。
    await ref.read(sessionsControllerProvider.notifier).refresh();
  }

  Future<void> _onRollback() async {
    final n = await showDialog<int>(
      context: context,
      builder: (_) => const _RollbackDialog(),
    );
    if (n == null || n <= 0) return;
    if (!mounted) return;
    try {
      final resp = await ref
          .read(chatControllerProvider(_sid).notifier)
          .rollback(n);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '已删除最后 $n 轮（共 ${resp.deletedMessages} 条消息）',
          ),
        ),
      );
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('回滚失败：$e')),
      );
    }
  }

  /// 进入「修改最后一次提问」编辑态：标记 _editingMessageId，让 _MessagesList
  /// 把这条 user 气泡渲染成 [_EditableUserBubble]（inline TextField + 右下角发送/
  /// 取消图标）。底部 composer 不受影响。
  void _onEditLastUserMessage(MessageOut userMsg) {
    setState(() => _editingMessageId = userMsg.id);
  }

  void _onCancelEdit() {
    setState(() => _editingMessageId = null);
  }

  /// 编辑态气泡内点发送：rollback 最后 1 轮 + 用新内容 send。
  Future<void> _onSendEdited(String text) async {
    final controller = ref.read(chatControllerProvider(_sid).notifier);
    setState(() => _editingMessageId = null);
    try {
      await controller.editLastTurn(text);
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('修改失败：$e')),
      );
    }
  }

  /// 点 fork（图标或长按菜单）：基于最近 checkpoint 分叉出新会话并跳转过去。
  ///
  /// 2026-06-02：去掉原先「输入新问题」对话框 —— fork 现在纯粹是「复制当前对话到
  /// 一个新分支继续聊」。新会话带着 fork 前的历史（后端复制到 PG messages），用户
  /// 进去后直接在 composer 里继续提问即可。
  ///
  /// 精准分叉：把被点的 [userMsg] id 透传给后端 `up_to_message_id`，历史只复制到
  /// 这条提问所在回合末尾（含其答案）—— 点中间那条 = 截到那条，点最后一条 = 全量。
  Future<void> _onForkFromUserMessage(MessageOut userMsg) async {
    final controller = ref.read(chatControllerProvider(_sid).notifier);
    try {
      // fork 的 LangGraph 侧一律用最近 checkpoint（下轮 send 会按 PG 历史重建，
      // checkpoint 精度不影响行为）；历史截断由 up_to_message_id 在 PG 层完成。
      final list = await controller.listCheckpoints();
      final cp = list.items.isNotEmpty ? list.items.first : null;
      if (cp == null) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('当前会话还没有可分叉的 checkpoint')),
        );
        return;
      }
      final created = await ref
          .read(sessionsControllerProvider.notifier)
          .fork(
            sid: _sid,
            checkpointId: cp.checkpointId,
            upToMessageId: userMsg.id,
          );
      if (!mounted) return;
      context.go('/sessions/${created.id}');
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('分叉失败：$e')),
      );
    }
  }

  Future<void> _onAssistantLongPress(MessageOut m) async {
    final action = await showModalBottomSheet<_AssistantAction>(
      context: context,
      showDragHandle: true,
      builder: (_) => const _AssistantMenuSheet(),
    );
    if (action == null) return;
    if (!mounted) return;
    switch (action) {
      case _AssistantAction.copy:
        // fire-and-forget：测试 / 平台 channel 未挂时 setData 可能抛
        // MissingPluginException；不让它阻塞 UI 反馈。
        Clipboard.setData(ClipboardData(text: m.content)).catchError(
          (Object _) {},
        );
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('已复制消息')),
        );
        break;
      case _AssistantAction.thumbUp:
        await _submitFeedback(m, 1);
        break;
      case _AssistantAction.thumbDown:
        await _submitFeedback(m, -1);
        break;
      case _AssistantAction.favorite:
        await _addFavorite(m);
        break;
      case _AssistantAction.note:
        await _addNote(m);
        break;
      case _AssistantAction.feedback:
        await _openFeedbackDialog(m);
        break;
    }
  }

  Future<void> _onUserLongPress(MessageOut m) async {
    final action = await showModalBottomSheet<_UserAction>(
      context: context,
      showDragHandle: true,
      builder: (_) => const _UserMenuSheet(),
    );
    if (action == null) return;
    if (!mounted) return;
    switch (action) {
      case _UserAction.copy:
        Clipboard.setData(ClipboardData(text: m.content)).catchError(
          (Object _) {},
        );
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('已复制消息')),
        );
        break;
      case _UserAction.forkFromHere:
        await _onForkFromUserMessage(m);
        break;
    }
  }

  Future<void> _submitFeedback(MessageOut m, int thumb) async {
    try {
      await ref
          .read(feedbackApiProvider)
          .upsert(m.id, thumb: thumb);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(thumb > 0 ? '已点赞' : '已点踩')),
      );
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('反馈失败：$e')),
      );
    }
  }

  Future<void> _openFeedbackDialog(MessageOut m) async {
    final result = await showDialog<_FeedbackResult>(
      context: context,
      builder: (_) => const _FeedbackDialog(),
    );
    if (result == null) return;
    if (!mounted) return;
    try {
      await ref
          .read(feedbackApiProvider)
          .upsert(m.id, thumb: result.thumb, reason: result.reason);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('反馈已提交')),
      );
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('反馈失败：$e')),
      );
    }
  }

  Future<void> _addFavorite(MessageOut m) async {
    try {
      await ref
          .read(favoritesApiProvider)
          .create(targetType: 'message', targetId: m.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('已收藏')),
      );
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('收藏失败：$e')),
      );
    }
  }

  Future<void> _addNote(MessageOut m) async {
    final body = await showDialog<String>(
      context: context,
      builder: (_) => const _NoteDialog(),
    );
    if (body == null || body.trim().isEmpty) return;
    if (!mounted) return;
    try {
      await ref.read(notesApiProvider).create(
            targetType: 'message',
            targetId: m.id,
            body: body.trim(),
          );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('已保存笔记')),
      );
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('保存笔记失败：$e')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final sid = _sid;
    final session = widget.session;
    final isArchived = session.isArchivedBranch;
    final stateAsync = ref.watch(chatControllerProvider(sid));

    ref.listen<AsyncValue<SessionChatState>>(
      chatControllerProvider(sid),
      (_, _) => _scrollToBottom(),
    );

    final state = stateAsync.value;
    final isRunning = state?.run.status == RunStatus.streaming ||
        state?.run.status == RunStatus.cancelling;
    final showPaused = _shouldShowPausedBanner(state);
    // 编辑入口可见性：非 archived + 非 run 中 + history 末尾形如 user+assistant。
    // 使用 chat_controller 的 isLastTurnEditable 同款逻辑保持一致。
    final lastEditableUserId =
        (!isArchived && state != null && !isRunning && !showPaused)
            ? _lastEditableUserMessageId(state)
            : null;
    // 编辑态气泡只能命中"当前可编辑的最后一条"——streaming/archived/paused 进来
    // 时 editableLastUserId 为 null，下面 ?: 自动失效。
    final editingMessageId = (_editingMessageId != null &&
            _editingMessageId == lastEditableUserId)
        ? _editingMessageId
        : null;

    return Column(
      children: [
        _Header(
          session: session,
          onRollback: isArchived ? null : _onRollback,
        ),
        const Divider(height: 1),
        Expanded(
          child: stateAsync.when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => Center(child: Text('加载失败：$e')),
            data: (s) => _MessagesList(
              scroll: _scroll,
              state: s,
              archived: isArchived,
              editableLastUserMessageId: lastEditableUserId,
              editingMessageId: editingMessageId,
              onAssistantLongPress: _onAssistantLongPress,
              onUserLongPress: _onUserLongPress,
              onEditLastUserMessage: _onEditLastUserMessage,
              onForkFromUserMessage: _onForkFromUserMessage,
              onSendEdited: _onSendEdited,
              onCancelEdit: _onCancelEdit,
            ),
          ),
        ),
        // 2026-05-31：原本独立一行的 NodeStatusStrip 已被消息列表里的
        // ReasoningPanel 取代（信息更丰富 + 嵌在「回答位置」更符合用户预期），
        // 不再在 Composer 上方单独渲染节点 chip。
        if (showPaused)
          _PausedBanner(onResume: isArchived ? null : _onResume),
        if (state?.run.status == RunStatus.error)
          _ErrorBanner(message: state!.run.errorMessage ?? 'unknown'),
        const Divider(height: 1),
        if (isArchived)
          _ArchivedBanner(session: session)
        else
          Composer(
            onSend: (text) {
              // 发出首条消息 → 该会话不再是空草稿，离开时不再被丢弃（Req2）。
              ref.read(sessionsControllerProvider.notifier).markUsed(sid);
              ref.read(chatControllerProvider(sid).notifier).send(text);
            },
            onCancel: () =>
                ref.read(chatControllerProvider(sid).notifier).cancel(),
            onPause: _onPause,
            onResume: _onResume,
            isRunning: isRunning,
            isPaused: showPaused,
          ),
      ],
    );
  }

  /// 末尾形如 `[..., user, assistant]` 时返回该 user message id；否则 null。
  /// 与 ChatController.isLastTurnEditable 等价（前端两侧都需要同一判断）。
  static String? _lastEditableUserMessageId(SessionChatState s) {
    final h = s.history;
    if (h.length < 2) return null;
    if (h[h.length - 2].role != 'user' || h[h.length - 1].role != 'assistant') {
      return null;
    }
    return h[h.length - 2].id;
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.session, this.onRollback});
  final SessionOut session;

  /// null → 不显示"删除最后 N 轮"入口（archived_branch 时禁用）。
  final VoidCallback? onRollback;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  session.displayTitle,
                  key: const Key('chat_header_title'),
                  style: Theme.of(context).textTheme.titleMedium,
                ),
              ],
            ),
          ),
          if (onRollback != null)
            PopupMenuButton<String>(
              key: const Key('chat_header_settings'),
              tooltip: '会话设置',
              icon: const Icon(Icons.more_vert),
              itemBuilder: (_) => const [
                PopupMenuItem(
                  value: 'rollback',
                  child: ListTile(
                    leading: Icon(Icons.history_toggle_off),
                    title: Text('删除最后 N 轮'),
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                  ),
                ),
              ],
              onSelected: (v) {
                if (v == 'rollback') onRollback?.call();
              },
            ),
        ],
      ),
    );
  }
}

class _MessagesList extends StatelessWidget {
  const _MessagesList({
    required this.scroll,
    required this.state,
    required this.archived,
    required this.editableLastUserMessageId,
    required this.editingMessageId,
    required this.onAssistantLongPress,
    required this.onUserLongPress,
    required this.onEditLastUserMessage,
    required this.onForkFromUserMessage,
    required this.onSendEdited,
    required this.onCancelEdit,
  });

  final ScrollController scroll;
  final SessionChatState state;
  final bool archived;

  /// 当前可被「修改最后一次提问」按钮覆盖的 user message id（null = 不显示按钮）。
  final String? editableLastUserMessageId;

  /// 当前处于 inline 编辑态的 user message id（非 null → 该气泡用
  /// [_EditableUserBubble] 渲染，并跳过长按 / 操作按钮叠加）。
  final String? editingMessageId;
  final Future<void> Function(MessageOut m) onAssistantLongPress;
  final Future<void> Function(MessageOut m) onUserLongPress;
  final void Function(MessageOut m) onEditLastUserMessage;

  /// 点 user 气泡上的 fork 图标（与长按菜单"从这里重问"等价）。
  final void Function(MessageOut m) onForkFromUserMessage;

  /// inline 编辑气泡内点发送（新文本，已 trim）。
  final void Function(String text) onSendEdited;

  /// inline 编辑气泡内点取消。
  final VoidCallback onCancelEdit;

  @override
  Widget build(BuildContext context) {
    final items = <Widget>[];
    for (final m in state.history) {
      // 答案完成后保留下来的 reasoning 折叠框（默认收起、可点开复盘）：渲染在
      // 对应 assistant 消息上方，与 streaming 期间的位置一致。仅本会话视图内刚跑
      // 完的轮次有快照；从 PG 重载的历史消息没有 → 不显示。
      if (m.role == 'assistant') {
        final snap = state.reasoningByMessageId[m.id];
        if (snap != null) {
          items.add(ReasoningPanel(
            key: ValueKey('reasoning-${m.id}'),
            nodes: snap.nodes,
            reasoningByNode: snap.reasoningByNode,
            activeNode: null,
            startedAt: null,
            collapsedFromController: true,
            frozenElapsed: snap.elapsed,
          ));
        }
      }
      // inline 编辑态：命中这条 user 消息 → 用 _EditableUserBubble 直接接管。
      // 跳过长按 / 操作按钮叠加，让编辑专心进行。
      if (m.role == 'user' && m.id == editingMessageId) {
        items.add(_EditableUserBubble(
          key: ValueKey('editable-user-bubble-${m.id}'),
          messageId: m.id,
          originalText: m.content,
          onSend: onSendEdited,
          onCancel: onCancelEdit,
        ));
        continue;
      }
      final bubble = MessageBubble(
        key: ValueKey('msg-${m.id}'),
        role: m.role,
        content: m.content,
        status: m.status,
        citations: m.citations,
      );
      // archived_branch 只读：长按菜单不响应（avoid fork-on-fork chains in MVP）
      if (archived) {
        items.add(bubble);
        continue;
      }
      Widget child = GestureDetector(
        behavior: HitTestBehavior.opaque,
        onLongPress: () {
          if (m.role == 'assistant') {
            onAssistantLongPress(m);
          } else if (m.role == 'user') {
            onUserLongPress(m);
          }
        },
        child: bubble,
      );
      // user message：气泡下方右对齐渲染两个纯图标按钮（在气泡外部，不与文字重叠）。
      // - 「分叉」按钮：所有 user message 都有（与长按菜单"从这里重问"等价）
      // - 「修改并重新提问」按钮：仅最后一条 user 且 last turn 可编辑时显示
      if (m.role == 'user') {
        final isLastEditable = m.id == editableLastUserMessageId;
        child = Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          mainAxisSize: MainAxisSize.min,
          children: [
            child,
            Padding(
              padding: const EdgeInsets.only(right: 16, top: 2, bottom: 4),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (isLastEditable)
                    IconButton(
                      key: ValueKey('msg-edit-${m.id}'),
                      onPressed: () => onEditLastUserMessage(m),
                      icon: const Icon(Icons.edit_outlined, size: 16),
                      tooltip: '修改并重新提问',
                      visualDensity: VisualDensity.compact,
                      padding: EdgeInsets.zero,
                      constraints: const BoxConstraints(
                        minWidth: 28,
                        minHeight: 28,
                      ),
                    ),
                  IconButton(
                    key: ValueKey('msg-fork-${m.id}'),
                    onPressed: () => onForkFromUserMessage(m),
                    icon: const Icon(Icons.fork_right, size: 16),
                    tooltip: '分叉',
                    visualDensity: VisualDensity.compact,
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(
                      minWidth: 28,
                      minHeight: 28,
                    ),
                  ),
                ],
              ),
            ),
          ],
        );
      }
      items.add(child);
    }
    final run = state.run;
    final showStreaming = run.status == RunStatus.streaming ||
        run.status == RunStatus.cancelling ||
        run.status == RunStatus.paused;
    // 即便 run.status 已切到 done / cancelled / error，只要本轮 reasoning 还在
    // run 状态里（_flushDoneToHistory 还没把它推进 history），也展示 reasoning
    // panel —— 让用户回看本轮是怎么思考的。
    final hasRunBubble = showStreaming;
    if (hasRunBubble) {
      if (run.userInput.isNotEmpty) {
        items.add(MessageBubble(
          key: const Key('msg-streaming-user'),
          role: 'user',
          content: run.userInput,
        ));
      }
      // ReasoningPanel 放在 streaming bubble 上方（在「回答的位置」）。任何时候
      // 只要有 nodes 或 hyde reasoning 累积都显示；空 → 自动 SizedBox.shrink。
      items.add(ReasoningPanel(
        key: const Key('reasoning_panel_inline'),
        nodes: run.nodes,
        reasoningByNode: run.reasoningByNode,
        activeNode: run.activeNode,
        startedAt: run.reasoningStartedAt,
        collapsedFromController: run.reasoningCollapsed,
      ));
      items.add(StreamingAssistantBubble(
        key: const Key('msg-streaming-assistant'),
        partial: run.partialAnswer,
      ));
    }

    if (items.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            '在下面输入框开始问答',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
        ),
      );
    }
    return ListView(
      key: const Key('messages_list'),
      controller: scroll,
      padding: const EdgeInsets.symmetric(vertical: 12),
      children: items,
    );
  }
}

class _PausedBanner extends StatelessWidget {
  const _PausedBanner({this.onResume});
  final VoidCallback? onResume;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      color: theme.colorScheme.tertiaryContainer,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(
        children: [
          Icon(Icons.pause_circle_filled,
              color: theme.colorScheme.onTertiaryContainer),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '已暂停 · 点击恢复继续生成',
              key: const Key('chat_paused_banner'),
              style: TextStyle(color: theme.colorScheme.onTertiaryContainer),
            ),
          ),
          if (onResume != null)
            FilledButton.icon(
              key: const Key('chat_paused_resume'),
              onPressed: onResume,
              icon: const Icon(Icons.play_arrow),
              label: const Text('恢复'),
            ),
        ],
      ),
    );
  }
}

class _ArchivedBanner extends ConsumerWidget {
  const _ArchivedBanner({required this.session});
  final SessionOut session;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final parentSid = session.forkedFromSessionId;
    return Container(
      width: double.infinity,
      color: theme.colorScheme.surfaceContainerHighest,
      padding: const EdgeInsets.all(16),
      child: Row(
        children: [
          Icon(Icons.fork_right, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(width: 8),
          const Expanded(
            child: Text(
              '这是从主线 fork 出的历史分支，只读。',
              key: Key('chat_archived_banner'),
              style: TextStyle(fontStyle: FontStyle.italic),
            ),
          ),
          if (parentSid != null && parentSid.isNotEmpty)
            OutlinedButton.icon(
              key: const Key('chat_archived_back_to_main'),
              onPressed: () => context.go('/sessions/$parentSid'),
              icon: const Icon(Icons.subdirectory_arrow_left),
              label: const Text('回到主线'),
            ),
        ],
      ),
    );
  }
}

/// 修改最后一次提问的 inline 编辑气泡（2026-06-02）。
///
/// 视觉沿用 [MessageBubble] user 分支：右对齐 + primaryContainer.withValues(0.4)
/// 背景 + outline border + borderRadius 14 + maxWidth 720。把原本的纯文本换成
/// 可编辑 [TextField]，气泡右下角内嵌「取消」「发送」两个图标按钮。
///
/// 键盘：Enter 发送；Shift+Enter 换行；Esc 取消。打开时 autofocus + 光标移到末尾。
/// 发送时回调 [onSend]，气泡侧不自己 clear / pop（父级 [_ChatViewState] 通过
/// setState `_editingMessageId = null` 切回普通气泡完成关闭）。
class _EditableUserBubble extends StatefulWidget {
  const _EditableUserBubble({
    super.key,
    required this.messageId,
    required this.originalText,
    required this.onSend,
    required this.onCancel,
  });

  final String messageId;
  final String originalText;
  final void Function(String text) onSend;
  final VoidCallback onCancel;

  @override
  State<_EditableUserBubble> createState() => _EditableUserBubbleState();
}

class _EditableUserBubbleState extends State<_EditableUserBubble> {
  late final TextEditingController _ctrl;
  late final FocusNode _focus;

  @override
  void initState() {
    super.initState();
    _ctrl = TextEditingController(text: widget.originalText);
    _focus = FocusNode(onKeyEvent: _onKey);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      _focus.requestFocus();
      _ctrl.selection = TextSelection.collapsed(offset: _ctrl.text.length);
    });
  }

  @override
  void dispose() {
    _ctrl.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _trySend() {
    final text = _ctrl.text.trim();
    if (text.isEmpty) return;
    widget.onSend(text);
  }

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (event.logicalKey == LogicalKeyboardKey.escape) {
      widget.onCancel();
      return KeyEventResult.handled;
    }
    if (event.logicalKey == LogicalKeyboardKey.enter ||
        event.logicalKey == LogicalKeyboardKey.numpadEnter) {
      if (HardwareKeyboard.instance.isShiftPressed) {
        return KeyEventResult.ignored;
      }
      _trySend();
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final canSend = _ctrl.text.trim().isNotEmpty;
    return Align(
      alignment: Alignment.centerRight,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 720),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 12),
          padding: const EdgeInsets.fromLTRB(14, 10, 8, 6),
          decoration: BoxDecoration(
            color: theme.colorScheme.primaryContainer.withValues(alpha: 0.4),
            border: Border.all(color: theme.colorScheme.primary),
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                key: Key('editable-input-${widget.messageId}'),
                controller: _ctrl,
                focusNode: _focus,
                minLines: 1,
                maxLines: 8,
                onChanged: (_) => setState(() {}),
                decoration: const InputDecoration(
                  isDense: true,
                  border: InputBorder.none,
                  enabledBorder: InputBorder.none,
                  focusedBorder: InputBorder.none,
                  contentPadding: EdgeInsets.zero,
                ),
                style: theme.textTheme.bodyMedium,
              ),
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  IconButton(
                    key: Key('editable-cancel-${widget.messageId}'),
                    onPressed: widget.onCancel,
                    icon: const Icon(Icons.close, size: 18),
                    tooltip: '取消',
                    visualDensity: VisualDensity.compact,
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(
                      minWidth: 32,
                      minHeight: 32,
                    ),
                  ),
                  IconButton(
                    key: Key('editable-send-${widget.messageId}'),
                    onPressed: canSend ? _trySend : null,
                    icon: const Icon(Icons.send, size: 18),
                    tooltip: '发送',
                    visualDensity: VisualDensity.compact,
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(
                      minWidth: 32,
                      minHeight: 32,
                    ),
                    color: theme.colorScheme.primary,
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      color: theme.colorScheme.errorContainer,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Text(
        '出错了：$message',
        key: const Key('chat_error_banner'),
        style: TextStyle(color: theme.colorScheme.onErrorContainer),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// 长按菜单 + 对话框（M5.4）
// ---------------------------------------------------------------------------

enum _AssistantAction { copy, thumbUp, thumbDown, favorite, note, feedback }

class _AssistantMenuSheet extends StatelessWidget {
  const _AssistantMenuSheet();

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              key: const Key('assistant_menu_copy'),
              leading: const Icon(Icons.content_copy),
              title: const Text('复制全文'),
              onTap: () => Navigator.pop(context, _AssistantAction.copy),
            ),
            ListTile(
              key: const Key('assistant_menu_thumb_up'),
              leading: const Icon(Icons.thumb_up_alt_outlined),
              title: const Text('点赞'),
              onTap: () => Navigator.pop(context, _AssistantAction.thumbUp),
            ),
            ListTile(
              key: const Key('assistant_menu_thumb_down'),
              leading: const Icon(Icons.thumb_down_alt_outlined),
              title: const Text('点踩'),
              onTap: () => Navigator.pop(context, _AssistantAction.thumbDown),
            ),
            ListTile(
              key: const Key('assistant_menu_favorite'),
              leading: const Icon(Icons.star_border),
              title: const Text('收藏'),
              onTap: () => Navigator.pop(context, _AssistantAction.favorite),
            ),
            ListTile(
              key: const Key('assistant_menu_note'),
              leading: const Icon(Icons.sticky_note_2_outlined),
              title: const Text('添加笔记'),
              onTap: () => Navigator.pop(context, _AssistantAction.note),
            ),
            ListTile(
              key: const Key('assistant_menu_feedback'),
              leading: const Icon(Icons.feedback_outlined),
              title: const Text('详细反馈'),
              onTap: () => Navigator.pop(context, _AssistantAction.feedback),
            ),
          ],
        ),
      ),
    );
  }
}

enum _UserAction { copy, forkFromHere }

class _UserMenuSheet extends StatelessWidget {
  const _UserMenuSheet();

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          ListTile(
            key: const Key('user_menu_copy'),
            leading: const Icon(Icons.content_copy),
            title: const Text('复制'),
            onTap: () => Navigator.pop(context, _UserAction.copy),
          ),
          ListTile(
            key: const Key('user_menu_fork'),
            leading: const Icon(Icons.fork_right),
            title: const Text('从这里分叉'),
            subtitle: const Text('复制当前对话到一个新会话继续'),
            onTap: () => Navigator.pop(context, _UserAction.forkFromHere),
          ),
        ],
      ),
    );
  }
}

class _RollbackDialog extends StatefulWidget {
  const _RollbackDialog();

  @override
  State<_RollbackDialog> createState() => _RollbackDialogState();
}

class _RollbackDialogState extends State<_RollbackDialog> {
  double _n = 1;

  @override
  Widget build(BuildContext context) {
    final n = _n.round();
    return AlertDialog(
      title: const Text('删除最后 N 轮消息'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('一轮 = 一条提问 + 对应的回答。此操作不可撤销，'
              '会同时删除对应的 LangGraph checkpoint。'),
          const SizedBox(height: 16),
          Slider(
            key: const Key('rollback_slider'),
            min: 1,
            max: 10,
            divisions: 9,
            value: _n,
            label: '$n',
            onChanged: (v) => setState(() => _n = v),
          ),
          Text(
            '将删除最后 $n 轮（约 ${n * 2} 条消息）',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
      actions: [
        TextButton(
          key: const Key('rollback_cancel'),
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('rollback_confirm'),
          style: FilledButton.styleFrom(
            backgroundColor: Theme.of(context).colorScheme.error,
          ),
          onPressed: () => Navigator.pop(context, n),
          child: const Text('删除'),
        ),
      ],
    );
  }
}

class _NoteDialog extends StatefulWidget {
  const _NoteDialog();

  @override
  State<_NoteDialog> createState() => _NoteDialogState();
}

class _NoteDialogState extends State<_NoteDialog> {
  final _ctrl = TextEditingController();

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('添加笔记'),
      content: TextField(
        key: const Key('note_input'),
        controller: _ctrl,
        autofocus: true,
        minLines: 3,
        maxLines: 8,
        decoration: const InputDecoration(
          hintText: '写下你的笔记…',
          border: OutlineInputBorder(),
        ),
      ),
      actions: [
        TextButton(
          key: const Key('note_cancel'),
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('note_confirm'),
          onPressed: () => Navigator.pop(context, _ctrl.text),
          child: const Text('保存'),
        ),
      ],
    );
  }
}

class _FeedbackResult {
  const _FeedbackResult({required this.thumb, this.reason});
  final int thumb;
  final String? reason;
}

class _FeedbackDialog extends StatefulWidget {
  const _FeedbackDialog();

  @override
  State<_FeedbackDialog> createState() => _FeedbackDialogState();
}

class _FeedbackDialogState extends State<_FeedbackDialog> {
  int _thumb = 1;
  final _reasonCtrl = TextEditingController();

  @override
  void dispose() {
    _reasonCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('详细反馈'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              ChoiceChip(
                key: const Key('feedback_thumb_up'),
                label: const Text('赞'),
                selected: _thumb == 1,
                onSelected: (_) => setState(() => _thumb = 1),
              ),
              const SizedBox(width: 8),
              ChoiceChip(
                key: const Key('feedback_thumb_down'),
                label: const Text('踩'),
                selected: _thumb == -1,
                onSelected: (_) => setState(() => _thumb = -1),
              ),
            ],
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('feedback_reason'),
            controller: _reasonCtrl,
            minLines: 2,
            maxLines: 6,
            decoration: const InputDecoration(
              labelText: '原因（可选）',
              border: OutlineInputBorder(),
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          key: const Key('feedback_cancel'),
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('feedback_confirm'),
          onPressed: () => Navigator.pop(
            context,
            _FeedbackResult(
              thumb: _thumb,
              reason: _reasonCtrl.text.trim().isEmpty
                  ? null
                  : _reasonCtrl.text.trim(),
            ),
          ),
          child: const Text('提交'),
        ),
      ],
    );
  }
}
