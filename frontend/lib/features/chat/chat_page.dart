import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/api/sessions_api.dart';
import '../../domain/session/sessions_controller.dart';
import 'chat_controller.dart';
import 'widgets/composer.dart';
import 'widgets/message_bubble.dart';
import 'widgets/node_status_strip.dart';

/// M5.2 起接通 SSE 流式问答。
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
                '开始一个新会话',
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
                label: const Text('新会话'),
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

  @override
  Widget build(BuildContext context) {
    final sid = widget.session.id;
    final isArchived = widget.session.isArchivedBranch;
    final stateAsync = ref.watch(chatControllerProvider(sid));

    ref.listen<AsyncValue<SessionChatState>>(
      chatControllerProvider(sid),
      (_, _) => _scrollToBottom(),
    );

    return Column(
      children: [
        _Header(session: widget.session),
        const Divider(height: 1),
        Expanded(
          child: stateAsync.when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => Center(child: Text('加载失败：$e')),
            data: (s) => _MessagesList(
              scroll: _scroll,
              state: s,
            ),
          ),
        ),
        stateAsync.maybeWhen(
          data: (s) => NodeStatusStrip(nodes: s.run.nodes),
          orElse: () => const SizedBox.shrink(),
        ),
        if (stateAsync.value?.run.status == RunStatus.error)
          _ErrorBanner(message: stateAsync.value!.run.errorMessage ?? 'unknown'),
        const Divider(height: 1),
        if (isArchived)
          const Padding(
            padding: EdgeInsets.all(16),
            child: Text(
              '这是从主线 fork 出的历史分支，只读。',
              style: TextStyle(fontStyle: FontStyle.italic),
            ),
          )
        else
          Composer(
            onSend: (text) => ref
                .read(chatControllerProvider(sid).notifier)
                .send(text, mode: _mode),
            onCancel: () =>
                ref.read(chatControllerProvider(sid).notifier).cancel(),
            isRunning: stateAsync.value?.run.isRunning ?? false,
            mode: _mode,
            onModeChanged: (m) => setState(() => _mode = m),
          ),
      ],
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.session});
  final SessionOut session;

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
        ],
      ),
    );
  }
}

class _MessagesList extends StatelessWidget {
  const _MessagesList({required this.scroll, required this.state});

  final ScrollController scroll;
  final SessionChatState state;

  @override
  Widget build(BuildContext context) {
    final items = <Widget>[];
    for (final m in state.history) {
      items.add(MessageBubble(
        key: ValueKey('msg-${m.id}'),
        role: m.role,
        content: m.content,
        status: m.status,
      ));
    }
    final run = state.run;
    if (run.status == RunStatus.streaming || run.status == RunStatus.cancelling) {
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
