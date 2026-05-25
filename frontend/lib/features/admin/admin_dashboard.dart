import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/langfuse_url.dart';
import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';
import 'widgets/docs_table.dart';
import 'widgets/rebuild_dialog.dart';
import 'widgets/tasks_panel.dart';
import 'widgets/usage_panel.dart';

/// `/admin` 入口。
///
/// 4 个 Tab：文档 / 任务 / 统计 / 工具（含重建索引按钮 + Langfuse 外链）。
///
/// 锚：`docs/03-development/05-frontend.md §0 M5.5` / §7。RBAC 入口
/// 在 `core/router.dart` redirect 与 `features/shell/app_shell.dart` sidebar
/// 双重隐藏，本页内自检 `role=admin`，非 admin 进来兜底显示 403 文案。
class AdminDashboard extends ConsumerStatefulWidget {
  const AdminDashboard({super.key});

  @override
  ConsumerState<AdminDashboard> createState() => _AdminDashboardState();
}

class _AdminDashboardState extends ConsumerState<AdminDashboard>
    with SingleTickerProviderStateMixin {
  late final TabController _tab;

  @override
  void initState() {
    super.initState();
    _tab = TabController(length: 4, vsync: this);
  }

  @override
  void dispose() {
    _tab.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final me = ref.watch(authControllerProvider).maybeWhen(
          data: (s) => s is AuthAuthenticated ? s.me : null,
          orElse: () => null,
        );

    if (me == null || me.role != 'admin') {
      // 兜底：非 admin 不应能到这里（router redirect 已挡），但仍提示。
      return Scaffold(
        appBar: AppBar(title: const Text('管理后台')),
        body: const Center(
          key: Key('admin_forbidden'),
          child: Padding(
            padding: EdgeInsets.all(24),
            child: Text(
              '没有访问权限。',
              textAlign: TextAlign.center,
            ),
          ),
        ),
      );
    }

    return Scaffold(
      appBar: AppBar(
        title: const Text('管理后台'),
        bottom: TabBar(
          key: const Key('admin_tab_bar'),
          controller: _tab,
          tabs: const [
            Tab(key: Key('admin_tab_docs'), text: '文档', icon: Icon(Icons.menu_book)),
            Tab(key: Key('admin_tab_tasks'), text: '任务', icon: Icon(Icons.task_alt)),
            Tab(key: Key('admin_tab_usage'), text: '统计', icon: Icon(Icons.insights)),
            Tab(key: Key('admin_tab_tools'), text: '工具', icon: Icon(Icons.build)),
          ],
        ),
      ),
      body: TabBarView(
        controller: _tab,
        children: const [
          DocsTable(),
          TasksPanel(),
          UsagePanel(),
          _ToolsTab(),
        ],
      ),
    );
  }
}

class _ToolsTab extends ConsumerWidget {
  const _ToolsTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        Card(
          child: ListTile(
            key: const Key('admin_rebuild_index_entry'),
            leading: const Icon(Icons.refresh),
            title: const Text('重建索引'),
            subtitle: const Text('按 spec_id 或全量重跑摄取流水线，结果以异步任务呈现'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () async {
              final created = await showDialog<bool>(
                context: context,
                builder: (_) => const RebuildIndexDialog(),
              );
              if (created == true && context.mounted) {
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(content: Text('已提交重建任务，可在"任务"页查看进度')),
                );
              }
            },
          ),
        ),
        const SizedBox(height: 8),
        Card(
          child: ListTile(
            key: const Key('admin_langfuse_link'),
            leading: const Icon(Icons.open_in_new),
            title: const Text('Langfuse 控制台'),
            subtitle: Text(LangfuseUrl.url),
            trailing: const Icon(Icons.chevron_right),
            onTap: () async {
              final uri = Uri.parse(LangfuseUrl.url);
              final ok = await launchUrl(
                uri,
                mode: LaunchMode.externalApplication,
              );
              if (!ok && context.mounted) {
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(content: Text('无法打开外链，请检查浏览器/Intent 配置')),
                );
              }
            },
          ),
        ),
      ],
    );
  }
}
