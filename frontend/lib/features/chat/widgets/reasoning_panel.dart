import 'package:flutter/material.dart';

import '../../../core/l10n/app_localizations.dart';
import '../chat_controller.dart';

/// reasoning 折叠框（2026-05-31）。
///
/// 用户提问到首个 token 到达期间，在消息列表底部（streaming bubble 上方）显示
/// 一个折叠框，展开态：
///   - 顶部：节点 chip 列（running 转圈/done 打勾）—— 沿用 NodeStatusStrip 视觉
///   - 底部：当前 active 节点的灰色滚动文字区
///       - hyde 节点：[ChatRunState.reasoningByNode]['hyde'] 字符级累积内容
///       - 其它节点：i18n placeholder「正在 X...」 / 节点 done 后的 summary 人话
///
/// 折叠态：单行 `已思考 X.Xs · N 步骤`，点击 + 上下箭头切换。
///
/// 自动折叠：[collapsedFromController] 由 [ChatController] 在首个 `token` 事件
/// 到达时切 true → AnimatedSize 收起。用户手动展开后用 `_userOverride` 标记，
/// 不再被 controller 信号自动覆盖。
///
/// 协议锚点：`docs/03-development/03-agent.md §7` SSE `node_progress` 行；
/// `04-backend-api.md §4.2` reasoning 折叠框说明。
class ReasoningPanel extends StatefulWidget {
  const ReasoningPanel({
    super.key,
    required this.nodes,
    required this.reasoningByNode,
    required this.activeNode,
    required this.startedAt,
    required this.collapsedFromController,
    this.frozenElapsed,
  });

  final List<NodeRunStatus> nodes;
  final Map<String, String> reasoningByNode;
  final String? activeNode;
  final DateTime? startedAt;
  final bool collapsedFromController;

  /// 答案完成后的历史快照用：折叠态「已思考 X.Xs」显示这个固定值，而不是用
  /// [startedAt] 跟 `DateTime.now()` 实时算（否则每次 rebuild 秒数会一直往上跳）。
  /// streaming 中为 null → 走实时计时。
  final Duration? frozenElapsed;

  @override
  State<ReasoningPanel> createState() => _ReasoningPanelState();
}

class _ReasoningPanelState extends State<ReasoningPanel> {
  /// 用户手动展开/折叠后接管自动控制：null 跟随 controller，true/false 覆盖。
  bool? _userOverride;

  final ScrollController _scroll = ScrollController();

  @override
  void didUpdateWidget(covariant ReasoningPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    // hyde 流式累积时把滚动条钉到底部
    if (widget.activeNode == 'hyde' &&
        widget.reasoningByNode['hyde'] !=
            oldWidget.reasoningByNode['hyde']) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted || !_scroll.hasClients) return;
        _scroll.jumpTo(_scroll.position.maxScrollExtent);
      });
    }
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  bool get _isCollapsed => _userOverride ?? widget.collapsedFromController;

  /// 折叠态显示的「已思考 X.Xs」依赖 parent 自然 rebuild —— 真路径下 token 流
  /// 持续到达让 ChatPage 频繁 rebuild，[ReasoningPanel] 跟着 rebuild，秒数随
  /// 之刷新。空 [startedAt] → 0.0；不另起 [Timer.periodic] 避免在 widget test
  /// 里让 `pumpAndSettle` 永不安定。
  void _toggle() {
    setState(() {
      _userOverride = !_isCollapsed;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (widget.nodes.isEmpty && widget.reasoningByNode.isEmpty) {
      return const SizedBox.shrink();
    }
    final theme = Theme.of(context);
    return Container(
      key: const Key('reasoning_panel'),
      margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 12),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerLow,
        border: Border.all(
          color: theme.colorScheme.outlineVariant,
        ),
        borderRadius: BorderRadius.circular(12),
      ),
      // 注：原来用 AnimatedSize 做折叠/展开过渡，但在 widget test 下
      // pumpAndSettle 偶发不稳（layout pass 反复 schedule frame）。改为直接
      // 切换无动画 —— 体验上从「灰条 → 单行收起」180ms 的滑动差别极小，但
      // 测试稳定性收益明显。
      child: _isCollapsed ? _buildCollapsed(context) : _buildExpanded(context),
    );
  }

  Widget _buildCollapsed(BuildContext context) {
    final theme = Theme.of(context);
    final l = AppLocalizations.of(context);
    final seconds = _elapsedSeconds();
    final steps = widget.nodes.length;
    return InkWell(
      key: const Key('reasoning_panel_collapsed'),
      borderRadius: BorderRadius.circular(12),
      onTap: _toggle,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        child: Row(
          children: [
            Icon(
              Icons.psychology_outlined,
              size: 16,
              color: theme.colorScheme.onSurfaceVariant,
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                l.reasoningCollapsedTitle(seconds, steps),
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                  fontStyle: FontStyle.italic,
                ),
              ),
            ),
            Icon(
              Icons.keyboard_arrow_down,
              size: 18,
              color: theme.colorScheme.onSurfaceVariant,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildExpanded(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 10, 8, 6),
          child: _ChipRow(nodes: widget.nodes, onToggle: _toggle),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
          child: Container(
            constraints: const BoxConstraints(maxHeight: 120),
            decoration: BoxDecoration(
              border: Border(
                top: BorderSide(color: theme.colorScheme.outlineVariant),
              ),
            ),
            padding: const EdgeInsets.only(top: 8),
            child: SingleChildScrollView(
              controller: _scroll,
              child: _ReasoningText(
                nodes: widget.nodes,
                reasoningByNode: widget.reasoningByNode,
                activeNode: widget.activeNode,
              ),
            ),
          ),
        ),
      ],
    );
  }

  String _elapsedSeconds() {
    final frozen = widget.frozenElapsed;
    if (frozen != null) {
      return (frozen.inMilliseconds / 1000.0).toStringAsFixed(1);
    }
    final start = widget.startedAt;
    if (start == null) return '0.0';
    final diff = DateTime.now().difference(start);
    final s = diff.inMilliseconds / 1000.0;
    return s.toStringAsFixed(1);
  }
}

/// 顶部 chip 列：节点状态可视化。沿用 M5.2 节点 chip 的视觉（running 转圈 /
/// done 打勾），去掉点击 sheet（在 reasoning 里点 chip 切换折叠不直观；折叠按钮
/// 放到右侧 trailing icon 上）。
class _ChipRow extends StatelessWidget {
  const _ChipRow({required this.nodes, required this.onToggle});
  final List<NodeRunStatus> nodes;
  final VoidCallback onToggle;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        Expanded(
          child: SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: [
                for (final n in nodes)
                  Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: Chip(
                      key: Key('reasoning_chip_${n.node}'),
                      visualDensity: VisualDensity.compact,
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
                        nodeLabel(n.node),
                        style: theme.textTheme.bodySmall,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
        IconButton(
          key: const Key('reasoning_panel_collapse_btn'),
          tooltip: AppLocalizations.of(context).reasoningCollapse,
          icon: const Icon(Icons.keyboard_arrow_up, size: 18),
          onPressed: onToggle,
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(
            minWidth: 28,
            minHeight: 28,
          ),
        ),
      ],
    );
  }
}

/// 灰色滚动文字区：当前 active 节点 → 流式 / placeholder；已 done 节点 → summary 人话。
class _ReasoningText extends StatelessWidget {
  const _ReasoningText({
    required this.nodes,
    required this.reasoningByNode,
    required this.activeNode,
  });
  final List<NodeRunStatus> nodes;
  final Map<String, String> reasoningByNode;
  final String? activeNode;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final style = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onSurfaceVariant,
      fontStyle: FontStyle.italic,
      height: 1.5,
    );

    final lines = _composeLines(context);
    if (lines.isEmpty) {
      return Text(
        AppLocalizations.of(context).reasoningWaiting,
        key: const Key('reasoning_waiting'),
        style: style,
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final line in lines)
          Padding(
            padding: const EdgeInsets.only(bottom: 4),
            child: Text(line, style: style),
          ),
      ],
    );
  }

  /// 拼接展示行：done 节点（summary 人话）→ active 节点（streaming/placeholder）。
  /// 顺序按 `nodes` 进入顺序保留，让用户复盘整条思路。
  List<String> _composeLines(BuildContext context) {
    final out = <String>[];
    for (final n in nodes) {
      final line = _lineForNode(context, n);
      if (line != null && line.isNotEmpty) out.add(line);
    }
    return out;
  }

  String? _lineForNode(BuildContext context, NodeRunStatus n) {
    final l = AppLocalizations.of(context);
    final prefix = nodeLabel(n.node);
    // active 且为 hyde 时，优先显示字符流累积；其它 active 节点显示 placeholder
    if (n.running) {
      if (n.node == 'hyde') {
        final progressive = reasoningByNode['hyde'] ?? '';
        if (progressive.isNotEmpty) {
          return '$prefix:\n$progressive';
        }
      }
      return '$prefix...';
    }
    // done：调用 summary helper
    final done = formatNodeSummary(l, n.node, n.summary);
    if (done != null && done.isNotEmpty) return done;
    // hyde done 但 summary 没字段 → 用累积的 reasoning 文本兜底
    if (n.node == 'hyde') {
      final progressive = reasoningByNode['hyde'] ?? '';
      if (progressive.isNotEmpty) return '$prefix:\n$progressive';
    }
    return prefix;
  }
}

/// 节点显示名 = 英文技术名（classify / rewrite / hyde / multi_query /
/// retrieve / rerank / generate / self_rag / tool_dispatch）。
///
/// 2026-06-01：用户要求步骤名用英文原名，而不是「改写问题 / 撰写假设答案」这类
/// 中文释义。后端只下发 `chat.py _NODE_NAMES` 白名单里的 9 个节点 key，直接原样
/// 展示即可——故不再走 i18n。节点下方的 summary「人话」仍是中文（见
/// [formatNodeSummary]），只是步骤名保持英文。
@visibleForTesting
String nodeLabel(String node) => node;

/// 把后端 `node_end.summary` 字段转成 reasoning 框里的「人话」一行。
///
/// 依据 [`backend/app/api/v1/chat.py`] `_summary_for_node_end`（2026-05-31 改造）：
/// - classify：`{query_class, complexity, rewritten_query?}`
/// - rewrite：`{rewritten_query?}`
/// - multi_query：`{sub_queries: [...]}`
/// - retrieve / rerank：`{candidates_count?, reranked_count?}`
/// - self_rag：`{self_rag_verdict, retry_count?, confidence?}`
/// - hyde：`{hyde_doc?}`（hyde 一般已经在流式 reasoningByNode 里展示，这里不重复）
///
/// 缺字段返回 null，让 caller fallback 到节点名 prefix。
@visibleForTesting
String? formatNodeSummary(
  AppLocalizations l,
  String node,
  Map<String, dynamic> summary,
) {
  switch (node) {
    case 'classify':
      final qc = summary['query_class']?.toString() ?? '';
      final cx = summary['complexity']?.toString() ?? '';
      final q = summary['rewritten_query']?.toString() ?? '';
      if (qc.isEmpty && cx.isEmpty && q.isEmpty) return null;
      return l.reasoningClassifyDone(qc, cx, q);
    case 'rewrite':
      final q = summary['rewritten_query']?.toString() ?? '';
      if (q.isEmpty) return null;
      return l.reasoningRewriteDone(q);
    case 'multi_query':
      final sub = (summary['sub_queries'] as List?)?.cast<Object>() ?? const [];
      if (sub.isEmpty) return null;
      final lines = ['${l.reasoningMultiQueryDone(sub.length)}:'];
      for (final s in sub) {
        lines.add('  - ${s.toString()}');
      }
      return lines.join('\n');
    case 'retrieve':
      final c = (summary['candidates_count'] as num?)?.toInt();
      if (c == null) return null;
      return l.reasoningRetrieveDone(c);
    case 'rerank':
      final c = (summary['reranked_count'] as num?)?.toInt();
      if (c == null) return null;
      return l.reasoningRerankDone(c);
    case 'self_rag':
      final v = summary['self_rag_verdict']?.toString() ?? '';
      final conf = (summary['confidence'] as num?)?.toDouble();
      if (v.isEmpty && conf == null) return null;
      final confStr = conf == null ? '-' : conf.toStringAsFixed(2);
      return l.reasoningSelfRagDone(v.isEmpty ? '?' : v, confStr);
    case 'hyde':
      final doc = summary['hyde_doc']?.toString() ?? '';
      if (doc.isEmpty) return null;
      // hyde 流式结束后 reasoningByNode 里已有完整内容；summary 只在 reasoning
      // text 没拿到 progress 时兜底（如直接走非流式 fallback）。
      return doc;
    default:
      return null;
  }
}
