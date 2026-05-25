import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/api/docs_api.dart';
import '../../data/api/sessions_api.dart';
import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';
import '../../domain/session/sessions_controller.dart';

/// 响应式 AppShell：
/// - 宽屏（>= 840）：固定左侧 Sidebar（含会话列表 + 新建按钮 + user/logout）
///   + 右侧主区（go_router child）
/// - 窄屏：AppBar + Drawer 抽屉化 Sidebar
///
/// 锚：`docs/03-development/05-frontend.md §4` / §10。
class AppShell extends ConsumerWidget {
  const AppShell({super.key, required this.child});

  /// docs §4 明确写："宽屏（>=840px）侧栏固定，窄屏侧栏抽屉化"。
  static const double wideBreakpoint = 840;

  final Widget child;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final isWide = constraints.maxWidth >= wideBreakpoint;
        if (isWide) {
          return Scaffold(
            body: Row(
              children: [
                const SizedBox(
                  width: 280,
                  child: _SessionsSidebar(),
                ),
                const VerticalDivider(width: 1, thickness: 1),
                Expanded(child: child),
              ],
            ),
          );
        }
        return Scaffold(
          appBar: AppBar(
            title: const Text('3GPP Everything'),
          ),
          drawer: const Drawer(
            width: 300,
            child: SafeArea(child: _SessionsSidebar()),
          ),
          body: child,
        );
      },
    );
  }
}

class _SessionsSidebar extends ConsumerWidget {
  const _SessionsSidebar();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final sessionsAsync = ref.watch(sessionsControllerProvider);
    final me = ref.watch(authControllerProvider).maybeWhen(
          data: (s) => s is AuthAuthenticated ? s.me : null,
          orElse: () => null,
        );
    final currentSid = _currentSidFromRoute(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const _SidebarHeader(),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 4, 12, 4),
          child: FilledButton.icon(
            key: const Key('sidebar_new_session'),
            onPressed: () => _onCreate(context, ref),
            icon: const Icon(Icons.add),
            label: const Text('新会话'),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: OutlinedButton.icon(
            key: const Key('sidebar_open_reader'),
            onPressed: () => _onOpenReader(context, ref),
            icon: const Icon(Icons.menu_book_outlined),
            label: const Text('阅读器'),
          ),
        ),
        // 仅 admin 可见的管理后台入口（M5.5）。后端 `/admin/*` 403 是兜底防线。
        if (me?.role == 'admin')
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
            child: OutlinedButton.icon(
              key: const Key('sidebar_open_admin'),
              onPressed: () {
                _closeDrawerIfOpen(context);
                context.go('/admin');
              },
              icon: const Icon(Icons.admin_panel_settings_outlined),
              label: const Text('管理后台'),
            ),
          ),
        const Divider(height: 1),
        Expanded(
          child: sessionsAsync.when(
            data: (items) => _SessionList(
              items: items,
              currentSid: currentSid,
              onTap: (s) => _onTapSession(context, s),
              onRename: (s) => _onRename(context, ref, s),
              onDelete: (s) => _onDelete(context, ref, s),
            ),
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => _SidebarError(
              message: '$e',
              onRetry: () => ref.read(sessionsControllerProvider.notifier).refresh(),
            ),
          ),
        ),
        const Divider(height: 1),
        _SidebarFooter(
          username: me?.username,
          role: me?.role,
          onLogout: () => ref.read(authControllerProvider.notifier).logout(),
        ),
      ],
    );
  }

  String? _currentSidFromRoute(BuildContext context) {
    final state = GoRouterState.of(context);
    return state.pathParameters['sid'];
  }

  Future<void> _onOpenReader(BuildContext context, WidgetRef ref) async {
    _closeDrawerIfOpen(context);
    final picked = await showDialog<String>(
      context: context,
      builder: (_) => const _DocPickerDialog(),
    );
    if (picked == null || picked.isEmpty) return;
    if (!context.mounted) return;
    context.go('/reader/${Uri.encodeComponent(picked)}');
  }

  Future<void> _onCreate(BuildContext context, WidgetRef ref) async {
    try {
      final created =
          await ref.read(sessionsControllerProvider.notifier).createBlank();
      if (!context.mounted) return;
      _closeDrawerIfOpen(context);
      context.go('/sessions/${created.id}');
    } on Object catch (e) {
      _snack(context, '创建会话失败：$e');
    }
  }

  void _onTapSession(BuildContext context, SessionOut s) {
    _closeDrawerIfOpen(context);
    context.go('/sessions/${s.id}');
  }

  Future<void> _onRename(
    BuildContext context,
    WidgetRef ref,
    SessionOut s,
  ) async {
    final controller = TextEditingController(text: s.title);
    final newTitle = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('重命名会话'),
        content: TextField(
          key: const Key('rename_input'),
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(labelText: '新标题'),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const Key('rename_confirm'),
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    if (newTitle == null || newTitle.isEmpty || newTitle == s.title) return;
    try {
      await ref.read(sessionsControllerProvider.notifier).rename(s.id, newTitle);
    } on Object catch (e) {
      if (context.mounted) _snack(context, '重命名失败：$e');
    }
  }

  Future<void> _onDelete(
    BuildContext context,
    WidgetRef ref,
    SessionOut s,
  ) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('删除会话'),
        content: Text('确认删除「${s.displayTitle}」？此操作不可撤销。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const Key('delete_confirm'),
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    if (!context.mounted) return;
    final currentSid = _currentSidFromRoute(context);
    try {
      await ref.read(sessionsControllerProvider.notifier).delete(s.id);
      if (!context.mounted) return;
      if (currentSid == s.id) {
        context.go('/chat');
      }
    } on Object catch (e) {
      if (context.mounted) _snack(context, '删除失败：$e');
    }
  }

  void _closeDrawerIfOpen(BuildContext context) {
    final scaffold = Scaffold.maybeOf(context);
    if (scaffold != null && scaffold.isDrawerOpen) {
      Navigator.of(context).pop();
    }
  }

  void _snack(BuildContext context, String msg) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }
}

/// 阅读器入口：从 `GET /docs` 拉已索引文档列表，按 spec_id 模糊过滤，
/// 选中后 Navigator.pop(specId) → AppShell 把它 push 进 `/reader/{spec}`。
class _DocPickerDialog extends ConsumerStatefulWidget {
  const _DocPickerDialog();

  @override
  ConsumerState<_DocPickerDialog> createState() => _DocPickerDialogState();
}

class _DocPickerDialogState extends ConsumerState<_DocPickerDialog> {
  final TextEditingController _filterCtrl = TextEditingController();
  String _filter = '';

  @override
  void dispose() {
    _filterCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(_docsListProvider);
    return AlertDialog(
      title: const Text('选择文档'),
      content: SizedBox(
        width: 420,
        height: 460,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              key: const Key('doc_picker_filter'),
              controller: _filterCtrl,
              decoration: const InputDecoration(
                isDense: true,
                hintText: '按 spec_id 过滤（如 23.501）',
                prefixIcon: Icon(Icons.search, size: 18),
              ),
              onChanged: (v) => setState(() => _filter = v.trim().toLowerCase()),
            ),
            const SizedBox(height: 12),
            Expanded(
              child: async.when(
                loading: () => const Center(child: CircularProgressIndicator()),
                error: (e, _) => Center(
                  child: Padding(
                    padding: const EdgeInsets.all(8),
                    child: Text(
                      '加载文档列表失败：$e',
                      key: const Key('doc_picker_error'),
                      textAlign: TextAlign.center,
                    ),
                  ),
                ),
                data: (resp) {
                  final filtered = _filter.isEmpty
                      ? resp.items
                      : resp.items
                          .where(
                            (d) => d.specId.toLowerCase().contains(_filter) ||
                                d.title.toLowerCase().contains(_filter),
                          )
                          .toList();
                  if (filtered.isEmpty) {
                    return Center(
                      child: Text(
                        resp.items.isEmpty ? '还没有任何已索引文档' : '没有匹配的文档',
                        key: const Key('doc_picker_empty'),
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    );
                  }
                  return ListView.builder(
                    key: const Key('doc_picker_list'),
                    itemCount: filtered.length,
                    itemBuilder: (ctx, i) {
                      final d = filtered[i];
                      return ListTile(
                        key: Key('doc_picker_tile_${d.specId}'),
                        dense: true,
                        title: Text(d.specId),
                        subtitle: Text(
                          '${d.release} · series ${d.series} · ${d.chunkCount} chunks',
                          style: const TextStyle(fontSize: 11),
                        ),
                        onTap: () => Navigator.of(context).pop(d.specId),
                      );
                    },
                  );
                },
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          key: const Key('doc_picker_cancel'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
      ],
    );
  }
}

final _docsListProvider =
    FutureProvider.autoDispose<DocListResponse>((ref) async {
  return ref.watch(docsApiProvider).list();
});

class _SidebarHeader extends StatelessWidget {
  const _SidebarHeader();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
      child: Row(
        children: [
          Icon(
            Icons.menu_book_outlined,
            color: Theme.of(context).colorScheme.primary,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '3GPP Everything',
              style: Theme.of(context).textTheme.titleMedium,
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
    );
  }
}

class _SidebarFooter extends StatelessWidget {
  const _SidebarFooter({
    required this.username,
    required this.role,
    required this.onLogout,
  });

  final String? username;
  final String? role;
  final VoidCallback onLogout;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  username ?? '-',
                  style: Theme.of(context).textTheme.bodyMedium,
                  overflow: TextOverflow.ellipsis,
                ),
                Text(
                  'role=${role ?? '-'}',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ],
            ),
          ),
          IconButton(
            key: const Key('sidebar_logout'),
            tooltip: '退出登录',
            onPressed: onLogout,
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
    );
  }
}

class _SidebarError extends StatelessWidget {
  const _SidebarError({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Text(
            '加载会话失败',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 8),
          Text(
            message,
            style: Theme.of(context).textTheme.bodySmall,
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 12),
          OutlinedButton(
            key: const Key('sidebar_retry'),
            onPressed: onRetry,
            child: const Text('重试'),
          ),
        ],
      ),
    );
  }
}

class _SessionList extends StatelessWidget {
  const _SessionList({
    required this.items,
    required this.currentSid,
    required this.onTap,
    required this.onRename,
    required this.onDelete,
  });

  final List<SessionOut> items;
  final String? currentSid;
  final void Function(SessionOut) onTap;
  final void Function(SessionOut) onRename;
  final void Function(SessionOut) onDelete;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(16),
        child: Center(
          child: Text(
            '还没有会话，点上方"新会话"开始。',
            style: Theme.of(context).textTheme.bodySmall,
            textAlign: TextAlign.center,
          ),
        ),
      );
    }

    final active = <SessionOut>[];
    final archived = <SessionOut>[];
    for (final s in items) {
      (s.isArchivedBranch ? archived : active).add(s);
    }

    return ListView(
      key: const Key('sessions_list'),
      padding: const EdgeInsets.symmetric(vertical: 8),
      children: [
        for (final s in active)
          _SessionTile(
            session: s,
            selected: s.id == currentSid,
            onTap: () => onTap(s),
            onRename: () => onRename(s),
            onDelete: () => onDelete(s),
          ),
        if (archived.isNotEmpty) ...[
          const Padding(
            padding: EdgeInsets.fromLTRB(16, 16, 16, 4),
            child: Text(
              '分叉历史',
              style: TextStyle(fontSize: 11, color: Colors.grey),
            ),
          ),
          for (final s in archived)
            _SessionTile(
              session: s,
              selected: s.id == currentSid,
              onTap: () => onTap(s),
              onRename: () => onRename(s),
              onDelete: () => onDelete(s),
            ),
        ],
      ],
    );
  }
}

class _SessionTile extends StatelessWidget {
  const _SessionTile({
    required this.session,
    required this.selected,
    required this.onTap,
    required this.onRename,
    required this.onDelete,
  });

  final SessionOut session;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback onRename;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final isArchived = session.isArchivedBranch;
    final fg = isArchived
        ? scheme.onSurface.withValues(alpha: 0.55)
        : scheme.onSurface;
    return Material(
      color: selected ? scheme.surfaceContainerHighest : Colors.transparent,
      child: ListTile(
        key: Key('session_tile_${session.id}'),
        dense: true,
        selected: selected,
        title: Text(
          session.displayTitle,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(color: fg, fontWeight: selected ? FontWeight.w600 : null),
        ),
        subtitle: isArchived
            ? Text(
                'archived',
                style: TextStyle(fontSize: 10, color: fg),
              )
            : null,
        onTap: onTap,
        trailing: PopupMenuButton<String>(
          key: Key('session_menu_${session.id}'),
          tooltip: '会话操作',
          itemBuilder: (_) => const [
            PopupMenuItem(value: 'rename', child: Text('重命名')),
            PopupMenuItem(value: 'delete', child: Text('删除')),
          ],
          onSelected: (v) {
            switch (v) {
              case 'rename':
                onRename();
                break;
              case 'delete':
                onDelete();
                break;
            }
          },
        ),
      ),
    );
  }
}
