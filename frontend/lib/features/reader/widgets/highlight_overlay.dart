import 'package:flutter/material.dart';

/// 一次性"高亮 → 淡出"的包装：进入时背景从 accent 透明度高 → 0，3s 内完成。
///
/// 用于 reader 锚点 `#chunk-{id}` 跳转后的瞬时视觉反馈，提示用户"就这块"。
class HighlightOverlay extends StatefulWidget {
  const HighlightOverlay({
    super.key,
    required this.child,
    required this.active,
    this.duration = const Duration(seconds: 3),
  });

  /// 被高亮包裹的内容。
  final Widget child;

  /// 改为 true 时触发一次高亮 → 淡出。再次变 true 会重新触发。
  final bool active;

  /// 整个淡出过程时长，默认 3s。
  final Duration duration;

  @override
  State<HighlightOverlay> createState() => _HighlightOverlayState();
}

class _HighlightOverlayState extends State<HighlightOverlay>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late Animation<double> _alpha;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(vsync: this, duration: widget.duration);
    _alpha = Tween<double>(begin: 0.32, end: 0.0)
        .chain(CurveTween(curve: Curves.easeOut))
        .animate(_ctrl);
    if (widget.active) _ctrl.forward(from: 0);
  }

  @override
  void didUpdateWidget(covariant HighlightOverlay old) {
    super.didUpdateWidget(old);
    if (widget.active && !old.active) {
      _ctrl.forward(from: 0);
    }
    if (widget.duration != old.duration) {
      _ctrl.duration = widget.duration;
    }
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return AnimatedBuilder(
      animation: _alpha,
      builder: (ctx, _) => Container(
        decoration: BoxDecoration(
          color: accent.withValues(alpha: _alpha.value),
          borderRadius: BorderRadius.circular(8),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
        child: widget.child,
      ),
    );
  }
}
