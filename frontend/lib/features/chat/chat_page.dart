import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';

/// M5.0 占位：M5.1 起会扩成 AppShell + 会话列表 + 真实聊天面板。
/// 当前页只为让登录后路由 redirect 有落点，并验证退出登录闭环。
class ChatPage extends ConsumerWidget {
  const ChatPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(authControllerProvider);
    final me = state.maybeWhen(
      data: (s) => s is AuthAuthenticated ? s.me : null,
      orElse: () => null,
    );

    return Scaffold(
      appBar: AppBar(
        title: const Text('Chat'),
        actions: [
          IconButton(
            key: const Key('logout_button'),
            tooltip: '退出登录',
            onPressed: () =>
                ref.read(authControllerProvider.notifier).logout(),
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('已登录：${me?.username ?? '-'} · role=${me?.role ?? '-'}'),
            const SizedBox(height: 16),
            const Text('M5.1 在装修中，会话与流式聊天即将上线。'),
          ],
        ),
      ),
    );
  }
}
