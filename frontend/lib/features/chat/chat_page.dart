import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/api/sessions_api.dart';
import '../../domain/session/sessions_controller.dart';

/// M5.1 占位聊天页。
///
/// - 无 sessionId（`/chat`）：欢迎/引导页，给个"创建第一个会话"按钮
/// - 有 sessionId（`/sessions/:sid`）：找到会话标题 + 占位文案；
///   找不到（已删除 / 拼错路径）→ 提示并提供回 `/chat` 入口
///
/// 真消息列表、Composer、SSE 流接入留给 M5.2。
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
        return _SessionPlaceholder(session: s);
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
                '从左侧选会话，或点下方按钮创建一个空会话。\nM5.2 起会接入流式问答。',
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

class _SessionPlaceholder extends StatelessWidget {
  const _SessionPlaceholder({required this.session});

  final SessionOut session;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            session.displayTitle,
            key: const Key('session_placeholder_title'),
            style: Theme.of(context).textTheme.headlineSmall,
          ),
          const SizedBox(height: 4),
          Text(
            'mode=${session.modeDefault} · status=${session.status}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
          const SizedBox(height: 24),
          Expanded(
            child: Center(
              child: Text(
                '聊天界面即将上线\nM5.2 会接入 SSE 流式问答 + 引用 chip',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyMedium,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
