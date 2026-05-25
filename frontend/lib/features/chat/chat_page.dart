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
import 'chat_controller.dart';
import 'widgets/composer.dart';
import 'widgets/message_bubble.dart';
import 'widgets/node_status_strip.dart';

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
        return _ChatView(session: s);
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
              const SizedBox(height: 8),
              Text(
                '从左侧选会话，或点下方按钮创建一个空会话。\n创建后可直接发问，agent 会流式回答 + 给引用。',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyMedium,
              ),
              const SizedBox(height: 24),
              FilledButton.icon(
                key: const Key('welcome_new_session'),
                onPressed: () => _onCreate(context, ref),
                icon: const Icon(Icons.add),
                label: Text(AppLocalizations.of(context).sidebarNewSession),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _onCreate(BuildContext context, WidgetRef ref) async {
    try {
      final created =
          await ref.read(sessionsControllerProvider.notifier).createBlank();
      if (!context.mounted) return;
      context.go('/sessions/${created.id}');
    } on Object catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('创建会话失败：$e')),
        );
      }
    }
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
  const _ChatView({required this.session});
  final SessionOut session;

  @override
  ConsumerState<_ChatView> createState() => _ChatViewState();
}

class _ChatViewState extends ConsumerState<_ChatView> {
  late String _mode;
  final ScrollController _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _mode = widget.session.modeDefault;
  }

  @override
  void dispose() {
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
        SnackBar(content: Text('已删除 ${resp.deletedMessages} 条消息')),
      );
    } on Object catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('回滚失败：$e')),
      );
    }
  }

  Future<void> _onForkFromUserMessage(MessageOut userMsg) async {
    final newQ = await showDialog<String>(
      context: context,
      builder: (_) => _ForkDialog(originalText: userMsg.content),
    );
    if (newQ == null || newQ.trim().isEmpty) return;
    if (!mounted) return;
    final controller = ref.read(chatControllerProvider(_sid).notifier);
    try {
      // M5.4 MVP：fork 一律用最近 checkpoint（messages 表未暴露 checkpoint_id）
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
            newUserMessage: newQ.trim(),
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
              onAssistantLongPress: _onAssistantLongPress,
              onUserLongPress: _onUserLongPress,
            ),
          ),
        ),
        stateAsync.maybeWhen(
          data: (s) => NodeStatusStrip(nodes: s.run.nodes),
          orElse: () => const SizedBox.shrink(),
        ),
        if (showPaused)
          _PausedBanner(onResume: isArchived ? null : _onResume),
        if (state?.run.status == RunStatus.error)
          _ErrorBanner(message: state!.run.errorMessage ?? 'unknown'),
        const Divider(height: 1),
        if (isArchived)
          _ArchivedBanner(session: session)
        else
          Composer(
            onSend: (text) => ref
                .read(chatControllerProvider(sid).notifier)
                .send(text, mode: _mode),
            onCancel: () =>
                ref.read(chatControllerProvider(sid).notifier).cancel(),
            onPause: _onPause,
            onResume: _onResume,
            isRunning: isRunning,
            isPaused: showPaused,
            mode: _mode,
            onModeChanged: (m) => setState(() => _mode = m),
          ),
      ],
    );
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
                const SizedBox(height: 2),
                Text(
                  'mode=${session.modeDefault} · status=${session.status}',
                  style: Theme.of(context).textTheme.bodySmall,
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
    required this.onAssistantLongPress,
    required this.onUserLongPress,
  });

  final ScrollController scroll;
  final SessionChatState state;
  final bool archived;
  final Future<void> Function(MessageOut m) onAssistantLongPress;
  final Future<void> Function(MessageOut m) onUserLongPress;

  @override
  Widget build(BuildContext context) {
    final items = <Widget>[];
    for (final m in state.history) {
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
      items.add(GestureDetector(
        behavior: HitTestBehavior.opaque,
        onLongPress: () {
          if (m.role == 'assistant') {
            onAssistantLongPress(m);
          } else if (m.role == 'user') {
            onUserLongPress(m);
          }
        },
        child: bubble,
      ));
    }
    final run = state.run;
    final showStreaming = run.status == RunStatus.streaming ||
        run.status == RunStatus.cancelling ||
        run.status == RunStatus.paused;
    if (showStreaming) {
      if (run.userInput.isNotEmpty) {
        items.add(MessageBubble(
          key: const Key('msg-streaming-user'),
          role: 'user',
          content: run.userInput,
        ));
      }
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
            title: const Text('从这里重问'),
            subtitle: const Text('用新问题分叉出一个新会话'),
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
    return AlertDialog(
      title: const Text('删除最后 N 轮消息'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('此操作不可撤销，会同时删除对应的 LangGraph checkpoint。'),
          const SizedBox(height: 16),
          Slider(
            key: const Key('rollback_slider'),
            min: 1,
            max: 10,
            divisions: 9,
            value: _n,
            label: '${_n.round()}',
            onChanged: (v) => setState(() => _n = v),
          ),
          Text(
            '将删除 ${_n.round()} 条消息',
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
          onPressed: () => Navigator.pop(context, _n.round()),
          child: const Text('删除'),
        ),
      ],
    );
  }
}

class _ForkDialog extends StatefulWidget {
  const _ForkDialog({required this.originalText});
  final String originalText;

  @override
  State<_ForkDialog> createState() => _ForkDialogState();
}

class _ForkDialogState extends State<_ForkDialog> {
  late final TextEditingController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = TextEditingController(text: widget.originalText);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('从这里重问'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            '将基于最近 checkpoint 分叉出新会话；原会话变成只读分叉历史。',
            style: TextStyle(fontSize: 12),
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('fork_input'),
            controller: _ctrl,
            autofocus: true,
            minLines: 2,
            maxLines: 6,
            decoration: const InputDecoration(
              labelText: '新问题',
              border: OutlineInputBorder(),
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          key: const Key('fork_cancel'),
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('fork_confirm'),
          onPressed: () => Navigator.pop(context, _ctrl.text),
          child: const Text('分叉'),
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
