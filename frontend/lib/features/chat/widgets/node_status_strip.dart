import 'package:flutter/material.dart';

import '../chat_controller.dart';

/// 节点状态条：渲染 chip 序列，running / done 颜色不同。
///
/// 来源锚点：`docs/03-development/05-frontend.md §5.2` —
/// running chip 用 accent；done chip 用灰阶；点击 chip 显示 duration / summary。
class NodeStatusStrip extends StatelessWidget {
  const NodeStatusStrip({super.key, required this.nodes});

  final List<NodeRunStatus> nodes;

  @override
  Widget build(BuildContext context) {
    if (nodes.isEmpty) return const SizedBox.shrink();
    final theme = Theme.of(context);
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: [
          for (final n in nodes)
            Padding(
              padding: const EdgeInsets.only(right: 6),
              child: InputChip(
                key: Key('node_chip_${n.node}'),
                avatar: SizedBox(
                  width: 14,
                  height: 14,
                  child: n.running
                      ? CircularProgressIndicator(
                          strokeWidth: 2,
                          valueColor: AlwaysStoppedAnimation(
                            theme.colorScheme.primary,
                          ),
                        )
                      : Icon(
                          Icons.check,
                          size: 14,
                          color: theme.colorScheme.onSurfaceVariant,
                        ),
                ),
                label: Text(
                  n.durationMs != null
                      ? '${n.node} · ${n.durationMs}ms'
                      : n.node,
                  style: theme.textTheme.bodySmall,
                ),
                onPressed: () => _showSummary(context, n),
              ),
            ),
        ],
      ),
    );
  }

  void _showSummary(BuildContext context, NodeRunStatus n) {
    if (n.summary.isEmpty) return;
    showModalBottomSheet<void>(
      context: context,
      builder: (c) => Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${n.node} · ${n.durationMs ?? 0}ms',
                style: Theme.of(c).textTheme.titleMedium),
            const SizedBox(height: 8),
            for (final e in n.summary.entries)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 2),
                child: Text('${e.key}: ${e.value}'),
              ),
          ],
        ),
      ),
    );
  }
}
