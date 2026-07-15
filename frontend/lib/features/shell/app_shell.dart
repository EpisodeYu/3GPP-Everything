import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/l10n/app_localizations.dart';
import '../../data/api/docs_api.dart';
import '../../data/api/sessions_api.dart';
import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';
import '../../domain/prefs/prefs_controller.dart';
import '../../domain/session/sessions_controller.dart';
import 'new_session_button.dart';

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
    // 全站可选中复制：把路由主内容包进 SelectionArea（Flutter web/CanvasKit 下
    // Text/Markdown 默认不可拖选）。放在这里（root Navigator 之下、有 Overlay 祖先）
    // 而非 MaterialApp.builder —— 后者在 Navigator 之上，SelectionArea 找不到
    // Overlay 会抛 "No Overlay widget found"。聊天气泡保留 `selectable:false`
    // （长按菜单复用其手势），SelectionArea 走拖选手势，两者不冲突。
    final selectableChild = SelectionArea(child: child);
    return LayoutBuilder(
      builder: (context, constraints) {
        final isWide = constraints.maxWidth >= wideBreakpoint;
        if (isWide) {
          return Scaffold(
            body: Row(
              children: [
                const SizedBox(width: 280, child: _SessionsSidebar()),
                const VerticalDivider(width: 1, thickness: 1),
                Expanded(child: selectableChild),
              ],
            ),
          );
        }
        return Scaffold(
          appBar: AppBar(title: const Text('3GPP Everything')),
          drawer: const Drawer(
            width: 300,
            child: SafeArea(child: _SessionsSidebar()),
          ),
          body: selectableChild,
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
    final me = ref
        .watch(authControllerProvider)
        .maybeWhen(
          data: (s) => s is AuthAuthenticated ? s.me : null,
          orElse: () => null,
        );
    final currentSid = _currentSidFromRoute(context);
    final t = AppLocalizations.of(context);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const _SidebarHeader(),
        const Padding(
          padding: EdgeInsets.fromLTRB(12, 4, 12, 4),
          child: NewSessionButton(buttonKey: Key('sidebar_new_session')),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: OutlinedButton.icon(
            key: const Key('sidebar_open_reader'),
            onPressed: () => _onOpenReader(context, ref),
            icon: const Icon(Icons.menu_book_outlined),
            label: Text(t.sidebarOpenReader),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: OutlinedButton.icon(
            key: const Key('sidebar_open_favorites'),
            onPressed: () {
              _closeDrawerIfOpen(context);
              context.push('/favorites');
            },
            icon: const Icon(Icons.bookmark_border),
            label: Text(t.sidebarOpenFavorites),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: OutlinedButton.icon(
            key: const Key('sidebar_open_notes'),
            onPressed: () {
              _closeDrawerIfOpen(context);
              context.push('/notes');
            },
            icon: const Icon(Icons.sticky_note_2_outlined),
            label: Text(t.sidebarOpenNotes),
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
              label: Text(t.sidebarOpenAdmin),
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
              onRetry: () =>
                  ref.read(sessionsControllerProvider.notifier).refresh(),
            ),
          ),
        ),
        // 仅在确实有会话时显示"清空全部"，避免空列表下 UI 噪声。
        sessionsAsync.maybeWhen(
          data: (items) => items.isEmpty
              ? const SizedBox.shrink()
              : Padding(
                  padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
                  child: TextButton.icon(
                    key: const Key('sidebar_delete_all'),
                    style: TextButton.styleFrom(
                      foregroundColor: Theme.of(context).colorScheme.error,
                    ),
                    onPressed: () => _onDeleteAll(context, ref, items.length),
                    icon: const Icon(Icons.delete_sweep_outlined, size: 18),
                    label: Text(t.sidebarDeleteAll),
                  ),
                ),
          orElse: () => const SizedBox.shrink(),
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
    // push（而非 go）：保留来源页在栈里，reader 返回时回到这里而非重置导航。
    context.push('/reader/${Uri.encodeComponent(picked)}');
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
    final t = AppLocalizations.of(context);
    final controller = TextEditingController(text: s.title);
    final newTitle = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.renameDialogTitle),
        content: TextField(
          key: const Key('rename_input'),
          controller: controller,
          autofocus: true,
          decoration: InputDecoration(labelText: t.renameDialogLabel),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: Text(t.renameDialogCancel),
          ),
          FilledButton(
            key: const Key('rename_confirm'),
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: Text(t.renameDialogSave),
          ),
        ],
      ),
    );
    if (newTitle == null || newTitle.isEmpty || newTitle == s.title) return;
    try {
      await ref
          .read(sessionsControllerProvider.notifier)
          .rename(s.id, newTitle);
    } on Object catch (e) {
      if (context.mounted) _snack(context, t.snackbarRenameFailed('$e'));
    }
  }

  Future<void> _onDeleteAll(
    BuildContext context,
    WidgetRef ref,
    int count,
  ) async {
    final t = AppLocalizations.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        key: const Key('delete_all_dialog'),
        title: Text(t.deleteAllDialogTitle),
        content: Text(t.deleteAllDialogContent(count)),
        actions: [
          TextButton(
            key: const Key('delete_all_cancel'),
            onPressed: () => Navigator.of(ctx).pop(false),
            child: Text(t.deleteDialogCancel),
          ),
          FilledButton(
            key: const Key('delete_all_confirm'),
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(t.deleteAllDialogConfirm),
          ),
        ],
      ),
    );
    if (ok != true) return;
    if (!context.mounted) return;
    _closeDrawerIfOpen(context);
    final currentSid = _currentSidFromRoute(context);
    try {
      final deleted = await ref
          .read(sessionsControllerProvider.notifier)
          .deleteAll();
      if (!context.mounted) return;
      // 当前停在某会话页 → 跳回欢迎页（路由参数对应的 session 已被清掉）
      if (currentSid != null) {
        context.go('/chat');
      }
      _snack(context, t.snackbarDeleteAllSuccess(deleted));
    } on Object catch (e) {
      if (context.mounted) _snack(context, t.snackbarDeleteAllFailed('$e'));
    }
  }

  Future<void> _onDelete(
    BuildContext context,
    WidgetRef ref,
    SessionOut s,
  ) async {
    final t = AppLocalizations.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(t.deleteDialogTitle),
        content: Text(t.deleteDialogContent(s.displayTitle)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: Text(t.deleteDialogCancel),
          ),
          FilledButton(
            key: const Key('delete_confirm'),
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.error,
            ),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(t.deleteDialogConfirm),
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
      if (context.mounted) _snack(context, t.snackbarDeleteFailed('$e'));
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
              onChanged: (v) =>
                  setState(() => _filter = v.trim().toLowerCase()),
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
                              (d) =>
                                  d.specId.toLowerCase().contains(_filter) ||
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
                          style: Theme.of(context).textTheme.bodySmall,
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

final _docsListProvider = FutureProvider.autoDispose<DocListResponse>((
  ref,
) async {
  // 同样等待鉴权状态恢复，避免首屏加载时发起未授权请求。
  final authState = await ref.watch(authControllerProvider.future);
  if (authState is! AuthAuthenticated) {
    return const DocListResponse(items: [], total: 0);
  }
  return ref.watch(docsApiProvider).list();
});

/// Sidebar header：title + 主题切换 + 语言切换。
///
/// 切换按钮放在 sidebar 顶部是为了让宽屏（无 AppBar）和窄屏（Drawer 抽出）
/// 都能就近触达，避免再在 ChatPage / ReaderPage 各自 Scaffold AppBar action 里
/// 重复一份。
class _SidebarHeader extends ConsumerWidget {
  const _SidebarHeader();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = AppLocalizations.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 8, 8),
      child: Row(
        children: [
          Icon(
            Icons.menu_book_outlined,
            color: Theme.of(context).colorScheme.primary,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              t.appTitle,
              style: Theme.of(context).textTheme.titleMedium,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          const _LanguageSwitcher(),
          const _ThemeSwitcher(),
        ],
      ),
    );
  }
}

class _ThemeSwitcher extends ConsumerWidget {
  const _ThemeSwitcher();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = AppLocalizations.of(context);
    final mode = ref.watch(prefsControllerProvider).themeMode;
    return PopupMenuButton<ThemeMode>(
      key: const Key('theme_switcher'),
      tooltip: t.themeTooltip,
      icon: Icon(switch (mode) {
        ThemeMode.light => Icons.light_mode_outlined,
        ThemeMode.dark => Icons.dark_mode_outlined,
        ThemeMode.system => Icons.brightness_auto_outlined,
      }),
      itemBuilder: (_) => [
        CheckedPopupMenuItem(
          key: const Key('theme_system'),
          value: ThemeMode.system,
          checked: mode == ThemeMode.system,
          child: Text(t.themeSystem),
        ),
        CheckedPopupMenuItem(
          key: const Key('theme_light'),
          value: ThemeMode.light,
          checked: mode == ThemeMode.light,
          child: Text(t.themeLight),
        ),
        CheckedPopupMenuItem(
          key: const Key('theme_dark'),
          value: ThemeMode.dark,
          checked: mode == ThemeMode.dark,
          child: Text(t.themeDark),
        ),
      ],
      onSelected: (m) =>
          ref.read(prefsControllerProvider.notifier).setThemeMode(m),
    );
  }
}

class _LanguageSwitcher extends ConsumerWidget {
  const _LanguageSwitcher();

  /// PopupMenuButton 把 `null` value 当作"用户取消"路径（不触发 onSelected），
  /// 因此用 sentinel 字符串 [_systemTag] 代表"跟随系统"，onSelected 里映射回 null。
  static const String _systemTag = '__system__';

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = AppLocalizations.of(context);
    final locale = ref.watch(prefsControllerProvider).locale;
    return PopupMenuButton<String>(
      key: const Key('language_switcher'),
      tooltip: t.languageTooltip,
      icon: const Icon(Icons.translate_outlined),
      itemBuilder: (_) => [
        CheckedPopupMenuItem<String>(
          key: const Key('language_system'),
          value: _systemTag,
          checked: locale == null,
          child: Text(t.themeSystem),
        ),
        CheckedPopupMenuItem<String>(
          key: const Key('language_en'),
          value: 'en',
          checked: locale?.languageCode == 'en',
          child: Text(t.languageEnglish),
        ),
        CheckedPopupMenuItem<String>(
          key: const Key('language_zh'),
          value: 'zh',
          checked: locale?.languageCode == 'zh',
          child: Text(t.languageChinese),
        ),
      ],
      onSelected: (tag) {
        final next = tag == _systemTag ? null : Locale(tag);
        ref.read(prefsControllerProvider.notifier).setLocale(next);
      },
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
    final t = AppLocalizations.of(context);
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
                  t.sidebarRoleLabel(role ?? '-'),
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ],
            ),
          ),
          IconButton(
            key: const Key('sidebar_logout'),
            tooltip: t.sidebarLogout,
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
    final t = AppLocalizations.of(context);
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Text(
            t.sidebarSessionsLoadError,
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
            child: Text(t.sidebarRetry),
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
    final t = AppLocalizations.of(context);
    if (items.isEmpty) {
      return Padding(
        padding: const EdgeInsets.all(16),
        child: Center(
          child: Text(
            t.sidebarSessionsEmpty,
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
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 4),
            child: Text(
              t.sidebarArchivedGroup,
              style: Theme.of(context).textTheme.labelMedium?.copyWith(
                color: Theme.of(context).colorScheme.onSurfaceVariant,
              ),
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
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final isArchived = session.isArchivedBranch;
    final fg = isArchived
        ? scheme.onSurface.withValues(alpha: 0.55)
        : scheme.onSurface;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 1),
      child: Material(
        color: selected
            ? scheme.primary.withValues(alpha: 0.10)
            : Colors.transparent,
        borderRadius: BorderRadius.circular(10),
        clipBehavior: Clip.antiAlias,
        child: ListTile(
          key: Key('session_tile_${session.id}'),
          dense: true,
          selected: selected,
          title: Text(
            session.displayTitle,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: theme.textTheme.bodyMedium?.copyWith(
              color: selected ? scheme.primary : fg,
              fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
            ),
          ),
          subtitle: isArchived
              ? Text(
                  'archived',
                  style: theme.textTheme.labelSmall?.copyWith(color: fg),
                )
              : null,
          onTap: onTap,
          trailing: PopupMenuButton<String>(
            key: Key('session_menu_${session.id}'),
            itemBuilder: (ctx) {
              final t = AppLocalizations.of(ctx);
              return [
                PopupMenuItem(
                  value: 'rename',
                  child: Text(t.sidebarSessionMenuRename),
                ),
                PopupMenuItem(
                  value: 'delete',
                  child: Text(t.sidebarSessionMenuDelete),
                ),
              ];
            },
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
      ),
    );
  }
}
