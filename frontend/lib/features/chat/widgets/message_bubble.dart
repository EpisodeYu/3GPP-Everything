import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_math_fork/flutter_math.dart';

/// 单条聊天消息气泡。User 与 Assistant 视觉区分：
/// - User：右对齐、accent 弱填充背景
/// - Assistant：左对齐、surfaceContainer 背景、markdown + 块级 LaTeX
///
/// LaTeX 仅识别块级 `$$ ... $$`；内联 `$ ... $` 留 M5.3 / M5.6 视语料反馈再加，
/// 避免 markdown 里的美元符号被误判（如 "$10"）。
class MessageBubble extends StatelessWidget {
  const MessageBubble({
    super.key,
    required this.role,
    required this.content,
    this.status = 'ok',
  });

  /// `'user'` | `'assistant'`。
  final String role;

  final String content;

  /// `'ok'` | `'cancelled'` | `'failed'`；非 ok 时角标提示。
  final String status;

  bool get _isUser => role == 'user';

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final bg = _isUser
        ? theme.colorScheme.primaryContainer.withValues(alpha: 0.4)
        : theme.colorScheme.surfaceContainer;
    final align = _isUser ? Alignment.centerRight : Alignment.centerLeft;
    final crossAlign =
        _isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start;

    return Align(
      alignment: align,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 720),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 12),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: bg,
            border: Border.all(color: theme.colorScheme.outline),
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(
            crossAxisAlignment: crossAlign,
            mainAxisSize: MainAxisSize.min,
            children: [
              if (status != 'ok')
                Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Text(
                    status == 'cancelled' ? '已取消' : '失败',
                    style: theme.textTheme.labelSmall?.copyWith(
                      color: theme.colorScheme.error,
                    ),
                  ),
                ),
              _MarkdownWithMath(text: content, isUser: _isUser),
            ],
          ),
        ),
      ),
    );
  }
}

/// 流式 token 累积态下的 assistant 气泡（带闪烁光标）。
class StreamingAssistantBubble extends StatelessWidget {
  const StreamingAssistantBubble({super.key, required this.partial});

  final String partial;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Align(
      alignment: Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 720),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 12),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: theme.colorScheme.surfaceContainer,
            border: Border.all(color: theme.colorScheme.outline),
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (partial.isEmpty)
                _typingDots(theme)
              else
                _MarkdownWithMath(text: partial, isUser: false),
            ],
          ),
        ),
      ),
    );
  }

  Widget _typingDots(ThemeData theme) {
    return SizedBox(
      width: 32,
      height: 16,
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          for (var i = 0; i < 3; i++)
            Container(
              width: 6,
              height: 6,
              decoration: BoxDecoration(
                color: theme.colorScheme.onSurfaceVariant,
                shape: BoxShape.circle,
              ),
            ),
        ],
      ),
    );
  }
}

/// 把文本按块级 `$$ ... $$` 切片，分别用 markdown / math 渲染后纵向堆叠。
class _MarkdownWithMath extends StatelessWidget {
  const _MarkdownWithMath({required this.text, required this.isUser});

  final String text;
  final bool isUser;

  static final RegExp _blockMath = RegExp(r'\$\$([\s\S]+?)\$\$');

  @override
  Widget build(BuildContext context) {
    final segments = _split(text);
    if (segments.length == 1 && segments.first is _MdSegment) {
      // 常态：没有公式，直接渲染一块 markdown
      return _md(context, (segments.first as _MdSegment).text);
    }
    return Column(
      crossAxisAlignment:
          isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final s in segments)
          if (s is _MdSegment)
            _md(context, s.text)
          else
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Math.tex(
                (s as _MathSegment).tex,
                mathStyle: MathStyle.display,
                onErrorFallback: (err) => SelectableText(
                  r'$$' + s.tex + r'$$',
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                    fontFamily: 'monospace',
                  ),
                ),
              ),
            ),
      ],
    );
  }

  Widget _md(BuildContext context, String md) {
    if (md.trim().isEmpty) return const SizedBox.shrink();
    return MarkdownBody(
      data: md,
      selectable: true,
      shrinkWrap: true,
    );
  }

  List<_Segment> _split(String input) {
    final out = <_Segment>[];
    var cursor = 0;
    for (final m in _blockMath.allMatches(input)) {
      if (m.start > cursor) {
        out.add(_MdSegment(input.substring(cursor, m.start)));
      }
      out.add(_MathSegment(m.group(1)!.trim()));
      cursor = m.end;
    }
    if (cursor < input.length) {
      out.add(_MdSegment(input.substring(cursor)));
    }
    if (out.isEmpty) out.add(_MdSegment(input));
    return out;
  }
}

sealed class _Segment {}

class _MdSegment extends _Segment {
  _MdSegment(this.text);
  final String text;
}

class _MathSegment extends _Segment {
  _MathSegment(this.tex);
  final String tex;
}
