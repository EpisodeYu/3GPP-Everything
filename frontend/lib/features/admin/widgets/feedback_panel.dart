import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/api/admin_api.dart';

/// 反馈页：从 `GET /admin/feedback` 拉用户点赞/点踩。
///
/// 顶部全量计数（赞/踩/合计）+ thumb 过滤（全部/👍/👎）+ 明细列表
/// （消息预览 + reason + 反馈者 + 时间）。管理员只读，不跳转他人会话。
final _feedbackProvider =
    FutureProvider.autoDispose.family<AdminFeedbackListResponse, int?>(
  (ref, thumb) => ref.watch(adminApiProvider).getFeedback(thumb: thumb),
);

class FeedbackPanel extends ConsumerStatefulWidget {
  const FeedbackPanel({super.key});

  @override
  ConsumerState<FeedbackPanel> createState() => _FeedbackPanelState();
}

class _FeedbackPanelState extends ConsumerState<FeedbackPanel> {
  /// null=全部 / 1=赞 / -1=踩。
  int? _filter;

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(_feedbackProvider(_filter));
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(_feedbackProvider(_filter)),
      child: async.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => ListView(
          padding: const EdgeInsets.all(24),
          children: [
            Center(
              key: const Key('admin_feedback_error'),
              child: Text('加载反馈失败：$e'),
            ),
            const SizedBox(height: 16),
            Center(
              child: OutlinedButton(
                key: const Key('admin_feedback_retry'),
                onPressed: () => ref.invalidate(_feedbackProvider(_filter)),
                child: const Text('重试'),
              ),
            ),
          ],
        ),
        data: (resp) => ListView(
          key: const Key('admin_feedback_list'),
          padding: const EdgeInsets.all(16),
          children: [
            _StatsRow(stats: resp.stats),
            const SizedBox(height: 12),
            _FilterChips(
              value: _filter,
              onChanged: (v) => setState(() => _filter = v),
            ),
            const SizedBox(height: 8),
            if (resp.items.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 48),
                child: Center(
                  key: Key('admin_feedback_empty'),
                  child: Text('没有反馈记录。'),
                ),
              )
            else
              for (final it in resp.items) _FeedbackTile(item: it),
          ],
        ),
      ),
    );
  }
}

class _StatsRow extends StatelessWidget {
  const _StatsRow({required this.stats});
  final FeedbackStatsOut stats;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        _StatCard(
          label: '点赞',
          value: '${stats.up}',
          icon: Icons.thumb_up,
          color: Colors.green,
          keyName: 'admin_feedback_up',
        ),
        const SizedBox(width: 8),
        _StatCard(
          label: '点踩',
          value: '${stats.down}',
          icon: Icons.thumb_down,
          color: Theme.of(context).colorScheme.error,
          keyName: 'admin_feedback_down',
        ),
        const SizedBox(width: 8),
        _StatCard(
          label: '合计',
          value: '${stats.total}',
          icon: Icons.forum_outlined,
          color: Theme.of(context).colorScheme.primary,
          keyName: 'admin_feedback_total',
        ),
      ],
    );
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({
    required this.label,
    required this.value,
    required this.icon,
    required this.color,
    required this.keyName,
  });
  final String label;
  final String value;
  final IconData icon;
  final Color color;
  final String keyName;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Card(
        key: Key(keyName),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(icon, size: 16, color: color),
                  const SizedBox(width: 6),
                  Text(label, style: Theme.of(context).textTheme.bodySmall),
                ],
              ),
              const SizedBox(height: 4),
              Text(value, style: Theme.of(context).textTheme.titleLarge),
            ],
          ),
        ),
      ),
    );
  }
}

class _FilterChips extends StatelessWidget {
  const _FilterChips({required this.value, required this.onChanged});
  final int? value;
  final ValueChanged<int?> onChanged;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 8,
      children: [
        ChoiceChip(
          key: const Key('admin_feedback_filter_all'),
          label: const Text('全部'),
          selected: value == null,
          onSelected: (_) => onChanged(null),
        ),
        ChoiceChip(
          key: const Key('admin_feedback_filter_up'),
          label: const Text('👍 点赞'),
          selected: value == 1,
          onSelected: (_) => onChanged(1),
        ),
        ChoiceChip(
          key: const Key('admin_feedback_filter_down'),
          label: const Text('👎 点踩'),
          selected: value == -1,
          onSelected: (_) => onChanged(-1),
        ),
      ],
    );
  }
}

class _FeedbackTile extends StatelessWidget {
  const _FeedbackTile({required this.item});
  final AdminFeedbackItem item;

  @override
  Widget build(BuildContext context) {
    final up = item.thumb > 0;
    final reason = item.reason;
    return Card(
      key: Key('admin_feedback_item_${item.id}'),
      child: ListTile(
        leading: Icon(
          up ? Icons.thumb_up : Icons.thumb_down,
          color: up ? Colors.green : Theme.of(context).colorScheme.error,
        ),
        title: Text(
          item.messagePreview ?? '（原消息已删除）',
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Text(
          [
            if (reason != null && reason.isNotEmpty) '理由：$reason',
            '${item.username ?? '?'} · ${item.createdAt}',
          ].join('\n'),
          maxLines: 3,
          overflow: TextOverflow.ellipsis,
        ),
        isThreeLine: reason != null && reason.isNotEmpty,
      ),
    );
  }
}
