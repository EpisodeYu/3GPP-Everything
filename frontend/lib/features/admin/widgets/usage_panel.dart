import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/api/admin_api.dart';

/// 统计页：从 `GET /admin/stats` 拉聚合数。
///
/// 展示：documents / chunks / users / sessions / messages / tasks 按 status 分桶 /
/// 最近 7 天 API 用量。
class UsagePanel extends ConsumerWidget {
  const UsagePanel({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(_statsProvider);
    return RefreshIndicator(
      onRefresh: () async => ref.invalidate(_statsProvider),
      child: async.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => ListView(
          padding: const EdgeInsets.all(24),
          children: [
            Center(
              key: const Key('admin_usage_error'),
              child: Text('加载统计失败：$e'),
            ),
            const SizedBox(height: 16),
            Center(
              child: OutlinedButton(
                key: const Key('admin_usage_retry'),
                onPressed: () => ref.invalidate(_statsProvider),
                child: const Text('重试'),
              ),
            ),
          ],
        ),
        data: (s) => ListView(
          key: const Key('admin_usage_list'),
          padding: const EdgeInsets.all(16),
          children: [
            Text('索引', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            _StatGrid(items: [
              _StatItem('文档数', '${s.documents}', key: 'admin_usage_documents'),
              _StatItem('chunk 数', '${s.chunks}', key: 'admin_usage_chunks'),
            ]),
            const SizedBox(height: 16),
            Text('用户', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            _StatGrid(items: [
              _StatItem('用户', '${s.users}', key: 'admin_usage_users'),
              _StatItem('会话', '${s.sessions}', key: 'admin_usage_sessions'),
              _StatItem('消息', '${s.messages}', key: 'admin_usage_messages'),
            ]),
            const SizedBox(height: 16),
            Text('任务（按 status 分桶）',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            _StatGrid(
              items: [
                for (final entry in s.tasks.entries)
                  _StatItem(entry.key, '${entry.value}',
                      key: 'admin_usage_tasks_${entry.key}'),
                if (s.tasks.isEmpty)
                  const _StatItem('—', '0', key: 'admin_usage_tasks_empty'),
              ],
            ),
            const SizedBox(height: 16),
            Text('近 7 天 API 用量',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            _StatGrid(items: [
              _StatItem('LLM 输入 token', '${s.apiUsage7d.llmInputTokens}',
                  key: 'admin_usage_llm_input'),
              _StatItem('LLM 输出 token', '${s.apiUsage7d.llmOutputTokens}',
                  key: 'admin_usage_llm_output'),
              _StatItem('embedding token', '${s.apiUsage7d.embeddingTokens}',
                  key: 'admin_usage_embedding'),
              _StatItem('rerank 次数', '${s.apiUsage7d.rerankCalls}',
                  key: 'admin_usage_rerank'),
              _StatItem('web 搜索次数', '${s.apiUsage7d.webSearchCalls}',
                  key: 'admin_usage_web'),
              _StatItem(
                '总成本（USD）',
                s.apiUsage7d.totalCostUsd.toStringAsFixed(4),
                key: 'admin_usage_cost',
              ),
            ]),
          ],
        ),
      ),
    );
  }
}

class _StatItem {
  const _StatItem(this.label, this.value, {required this.key});
  final String label;
  final String value;
  final String key;
}

class _StatGrid extends StatelessWidget {
  const _StatGrid({required this.items});
  final List<_StatItem> items;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, box) {
      final cols = box.maxWidth >= 800 ? 4 : (box.maxWidth >= 480 ? 2 : 1);
      return GridView.count(
        crossAxisCount: cols,
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        childAspectRatio: 2.4,
        mainAxisSpacing: 8,
        crossAxisSpacing: 8,
        children: [
          for (final it in items)
            Card(
              key: Key(it.key),
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Text(it.label,
                        style: Theme.of(context).textTheme.bodySmall),
                    const SizedBox(height: 4),
                    Text(it.value,
                        style: Theme.of(context).textTheme.titleLarge),
                  ],
                ),
              ),
            ),
        ],
      );
    });
  }
}

final _statsProvider = FutureProvider.autoDispose<StatsOut>(
  (ref) => ref.watch(adminApiProvider).getStats(),
);
