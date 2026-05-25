import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/api/admin_api.dart';

/// 任务面板（M5.5）：
///
/// - 列出最近任务（按 created_at desc）
/// - 状态过滤：queued / running / done / failed
/// - 自动轮询：每 3s 拉一次 `/admin/tasks`（仅当任一任务处于 queued / running 时启用），
///   单任务面板用户点开后切换到细粒度 polling
/// - 点开行 → 弹 bottom sheet 显示完整 log_tail（10 行）+ progress + payload
class TasksPanel extends ConsumerStatefulWidget {
  const TasksPanel({super.key});

  @override
  ConsumerState<TasksPanel> createState() => _TasksPanelState();
}

class _TasksPanelState extends ConsumerState<TasksPanel> {
  static const Duration _pollInterval = Duration(seconds: 3);

  String? _statusFilter;
  Timer? _timer;
  TaskListResponse? _last;
  Object? _err;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    setState(() => _loading = true);
    try {
      final resp = await ref.read(adminApiProvider).listTasks(
            statusFilter: _statusFilter,
          );
      if (!mounted) return;
      setState(() {
        _last = resp;
        _err = null;
        _loading = false;
      });
      _maybeSchedulePoll();
    } on Object catch (e) {
      if (!mounted) return;
      setState(() {
        _err = e;
        _loading = false;
      });
    }
  }

  void _maybeSchedulePoll() {
    _timer?.cancel();
    final items = _last?.items ?? const [];
    final hasInflight =
        items.any((t) => t.status == 'queued' || t.status == 'running');
    if (hasInflight) {
      _timer = Timer(_pollInterval, _refresh);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
          child: Row(
            children: [
              for (final s in const [null, 'queued', 'running', 'done', 'failed'])
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    key: Key('admin_tasks_filter_${s ?? 'all'}'),
                    label: Text(s ?? '全部'),
                    selected: _statusFilter == s,
                    onSelected: (sel) {
                      if (!sel) return;
                      setState(() => _statusFilter = s);
                      _refresh();
                    },
                  ),
                ),
              const Spacer(),
              IconButton(
                key: const Key('admin_tasks_refresh'),
                tooltip: '刷新',
                onPressed: _loading ? null : _refresh,
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(child: _buildBody(context)),
      ],
    );
  }

  Widget _buildBody(BuildContext context) {
    if (_err != null && _last == null) {
      return Center(
        key: const Key('admin_tasks_error'),
        child: Text('加载任务列表失败：$_err'),
      );
    }
    final items = _last?.items;
    if (items == null) {
      return const Center(child: CircularProgressIndicator());
    }
    if (items.isEmpty) {
      return const Center(
        key: Key('admin_tasks_empty'),
        child: Text('没有任务'),
      );
    }
    return ListView.separated(
      key: const Key('admin_tasks_list'),
      itemCount: items.length,
      separatorBuilder: (_, _) => const Divider(height: 1),
      itemBuilder: (_, i) {
        final t = items[i];
        return ListTile(
          key: Key('admin_tasks_row_${t.id}'),
          dense: true,
          leading: _StatusDot(status: t.status),
          title: Text('${t.kind} · ${t.status}'),
          subtitle: Text(
            'progress=${t.progress}% · 创建于 ${t.createdAt}'
            '${t.payload.isNotEmpty ? ' · payload=${t.payload}' : ''}',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
          ),
          trailing: SizedBox(
            width: 80,
            child: LinearProgressIndicator(value: t.progress / 100.0),
          ),
          onTap: () => _showDetail(context, t),
        );
      },
    );
  }

  void _showDetail(BuildContext context, TaskOut t) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (_) {
        return DraggableScrollableSheet(
          expand: false,
          initialChildSize: 0.55,
          maxChildSize: 0.9,
          builder: (_, scrollCtrl) => SingleChildScrollView(
            controller: scrollCtrl,
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('任务 ${t.id}',
                    key: const Key('admin_task_detail_id'),
                    style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                Text('kind: ${t.kind}'),
                Text('status: ${t.status}'),
                Text('progress: ${t.progress}%'),
                Text('created_at: ${t.createdAt}'),
                if (t.startedAt != null) Text('started_at: ${t.startedAt}'),
                if (t.finishedAt != null) Text('finished_at: ${t.finishedAt}'),
                Text('payload: ${t.payload}'),
                const SizedBox(height: 12),
                Text('log_tail',
                    style: Theme.of(context).textTheme.titleSmall),
                const SizedBox(height: 4),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(8),
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.surfaceContainer,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: SelectableText(
                    t.logTail.isEmpty ? '(empty)' : t.logTail,
                    key: const Key('admin_task_detail_log'),
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.status});
  final String status;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = switch (status) {
      'queued' => scheme.onSurfaceVariant,
      'running' => scheme.primary,
      'done' => Colors.green,
      'failed' => scheme.error,
      _ => scheme.outline,
    };
    return Container(
      width: 10,
      height: 10,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}
