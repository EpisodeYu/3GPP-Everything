import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/admin_api.dart';
import '../chat/widgets/message_bubble.dart';
import '../reader/widgets/highlight_overlay.dart';

/// Admin 只读会话查看页（`/admin/sessions/:sid?msg=<id>`）。
///
/// 从反馈 tab 点一条进来：展示该用户**整段对话**（含引用 chip，可跳阅读器），
/// 加载后滚到并高亮被反馈的那条消息，便于针对性优化（看清是检索还是生成的问题）。
final _sessionDetailProvider =
    FutureProvider.autoDispose.family<AdminSessionDetailOut, String>(
  (ref, sid) => ref.watch(adminApiProvider).getSessionDetail(sid),
);

class AdminSessionView extends ConsumerStatefulWidget {
  const AdminSessionView({
    super.key,
    required this.sessionId,
    this.highlightMessageId,
  });

  final String sessionId;
  final String? highlightMessageId;

  @override
  ConsumerState<AdminSessionView> createState() => _AdminSessionViewState();
}

class _AdminSessionViewState extends ConsumerState<AdminSessionView> {
  final GlobalKey _anchorKey = GlobalKey();
  bool _highlightTriggered = false;

  bool get _wantsHighlight =>
      widget.highlightMessageId != null && !_highlightTriggered;

  void _maybeScrollToHighlight() {
    if (!_wantsHighlight) return;
    final ctx = _anchorKey.currentContext;
    if (ctx == null) return;
    _highlightTriggered = true;
    Scrollable.ensureVisible(
      ctx,
      duration: const Duration(milliseconds: 350),
      curve: Curves.easeOut,
      alignment: 0.1,
    );
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(_sessionDetailProvider(widget.sessionId));
    if (_wantsHighlight) {
      WidgetsBinding.instance
          .addPostFrameCallback((_) => _maybeScrollToHighlight());
    }
    return Scaffold(
      appBar: AppBar(title: const Text('会话详情')),
      body: async.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text('加载会话失败：$e', key: const Key('admin_session_error')),
                const SizedBox(height: 12),
                OutlinedButton(
                  onPressed: () =>
                      ref.invalidate(_sessionDetailProvider(widget.sessionId)),
                  child: const Text('重试'),
                ),
              ],
            ),
          ),
        ),
        data: (detail) {
          final items = <Widget>[
            _Meta(detail: detail),
            const Divider(height: 1),
          ];
          for (final m in detail.messages) {
            final bubble = MessageBubble(
              key: ValueKey('admin-msg-${m.id}'),
              role: m.role,
              content: m.content,
              status: m.status,
              citations: m.citations,
            );
            if (widget.highlightMessageId != null &&
                m.id == widget.highlightMessageId) {
              items.add(HighlightOverlay(
                key: _anchorKey,
                active: _highlightTriggered,
                child: bubble,
              ));
            } else {
              items.add(bubble);
            }
          }
          if (detail.messages.isEmpty) {
            items.add(const Padding(
              padding: EdgeInsets.all(48),
              child: Center(child: Text('该会话没有消息。')),
            ));
          }
          return ListView(
            key: const Key('admin_session_messages'),
            padding: const EdgeInsets.symmetric(vertical: 12),
            children: items,
          );
        },
      ),
    );
  }
}

class _Meta extends StatelessWidget {
  const _Meta({required this.detail});
  final AdminSessionDetailOut detail;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            detail.title.isEmpty ? '（无标题会话）' : detail.title,
            key: const Key('admin_session_title'),
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 2),
          Text(
            '用户：${detail.username ?? '?'}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }
}
