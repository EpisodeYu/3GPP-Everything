import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// 聊天输入框 + 发送/取消按钮 + mode toggle。
///
/// 行为锚点：`docs/03-development/05-frontend.md §5.5`：
/// - 多行输入；Enter 发送，Shift+Enter 换行
/// - 跑起来后按钮变 "取消"
/// - 显式工具勾选（MVP 暂不接 UI，留 `onToolsChanged` 待 M5.4/M5.5）
class Composer extends StatefulWidget {
  const Composer({
    super.key,
    required this.onSend,
    required this.onCancel,
    required this.isRunning,
    this.mode = 'qa',
    this.onModeChanged,
  });

  /// 用户按 Enter / 点 Send 时回调，文本已 trim。
  final void Function(String text) onSend;

  /// 跑起来后点取消按钮回调。
  final VoidCallback onCancel;

  /// 当前是否有 run 在跑：true → 按钮显示 "取消"；false → "发送"。
  final bool isRunning;

  /// `'qa'` | `'raw_lookup'`。
  final String mode;

  /// 用户切 mode（QA / RawLookup）时回调。null → 不展示 toggle。
  final ValueChanged<String>? onModeChanged;

  @override
  State<Composer> createState() => _ComposerState();
}

class _ComposerState extends State<Composer> {
  late final TextEditingController _ctrl;
  late final FocusNode _focus;

  @override
  void initState() {
    super.initState();
    _ctrl = TextEditingController();
    _focus = FocusNode();
  }

  @override
  void dispose() {
    _ctrl.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _trySend() {
    final text = _ctrl.text.trim();
    if (text.isEmpty || widget.isRunning) return;
    widget.onSend(text);
    _ctrl.clear();
  }

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent) return KeyEventResult.ignored;
    if (event.logicalKey != LogicalKeyboardKey.enter &&
        event.logicalKey != LogicalKeyboardKey.numpadEnter) {
      return KeyEventResult.ignored;
    }
    final shift = HardwareKeyboard.instance.isShiftPressed;
    if (shift) {
      // 默认换行行为：让 TextField 处理
      return KeyEventResult.ignored;
    }
    _trySend();
    return KeyEventResult.handled;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final canSend = !widget.isRunning && _ctrl.text.trim().isNotEmpty;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (widget.onModeChanged != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Wrap(
                spacing: 8,
                children: [
                  ChoiceChip(
                    key: const Key('composer_mode_qa'),
                    label: const Text('QA'),
                    selected: widget.mode == 'qa',
                    onSelected: (_) => widget.onModeChanged?.call('qa'),
                  ),
                  ChoiceChip(
                    key: const Key('composer_mode_raw'),
                    label: const Text('RawLookup'),
                    selected: widget.mode == 'raw_lookup',
                    onSelected: (_) => widget.onModeChanged?.call('raw_lookup'),
                  ),
                ],
              ),
            ),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: Focus(
                  onKeyEvent: _onKey,
                  child: TextField(
                    key: const Key('composer_input'),
                    controller: _ctrl,
                    focusNode: _focus,
                    minLines: 1,
                    maxLines: 6,
                    enabled: !widget.isRunning,
                    onChanged: (_) => setState(() {}),
                    decoration: InputDecoration(
                      hintText: widget.isRunning ? '正在生成…' : '问点什么',
                      filled: true,
                      fillColor: theme.colorScheme.surfaceContainer,
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              if (widget.isRunning)
                FilledButton.icon(
                  key: const Key('composer_cancel'),
                  onPressed: widget.onCancel,
                  icon: const Icon(Icons.stop_circle_outlined),
                  label: const Text('取消'),
                )
              else
                FilledButton.icon(
                  key: const Key('composer_send'),
                  onPressed: canSend ? _trySend : null,
                  icon: const Icon(Icons.send),
                  label: const Text('发送'),
                ),
            ],
          ),
        ],
      ),
    );
  }
}
