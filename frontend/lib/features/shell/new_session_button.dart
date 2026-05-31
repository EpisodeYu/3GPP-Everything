import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/l10n/app_localizations.dart';
import '../../domain/session/sessions_controller.dart';

/// "新会话" 按钮：创建空会话（`POST /sessions`）并跳到该会话。
///
/// 创建是一次网络往返，期间按钮**禁用 + 显示 spinner**：
/// - 给即时反馈，避免高延迟下按钮看着像没反应；
/// - 防止用户连点重复触发 `createBlank` —— 否则一次连点会建出一堆空会话
///   并发多次跳转（历史 bug：点新会话弹不出窗口、要点好几次）。
///
/// sidebar 与 welcome 两处共用，仅按钮 [Key] 不同。
class NewSessionButton extends ConsumerStatefulWidget {
  const NewSessionButton({super.key, required this.buttonKey});

  /// 按钮自身的 [Key]（sidebar / welcome 各有测试锚点，需区分）。
  final Key buttonKey;

  @override
  ConsumerState<NewSessionButton> createState() => _NewSessionButtonState();
}

class _NewSessionButtonState extends ConsumerState<NewSessionButton> {
  bool _creating = false;

  /// 当前路由所在会话若仍是空草稿，返回它的 sid；否则 null。
  /// （welcome 页 / 测试里无 GoRouter 时安全返回 null。）
  String? _currentDraftSid(SessionsController sessions) {
    if (GoRouter.maybeOf(context) == null) return null;
    final sid = GoRouterState.of(context).pathParameters['sid'];
    if (sid == null || !sessions.isDraft(sid)) return null;
    return sid;
  }

  Future<void> _onCreate() async {
    if (_creating) return;
    final sessions = ref.read(sessionsControllerProvider.notifier);
    // Req1：已经在一个空草稿会话里 → 留在当前会话，不再新建一个空对话。
    final draftSid = _currentDraftSid(sessions);
    if (draftSid != null) {
      final s = Scaffold.maybeOf(context);
      if (s != null && s.isDrawerOpen) s.closeDrawer();
      context.go('/sessions/$draftSid');
      return;
    }
    setState(() => _creating = true);
    final t = AppLocalizations.of(context);
    final messenger = ScaffoldMessenger.of(context);
    // 窄屏：从抽屉里点的，跳转前先收起抽屉（State 引用，可跨 await 持有）。
    final scaffold = Scaffold.maybeOf(context);
    try {
      final created = await sessions.createBlank();
      if (!mounted) return;
      if (scaffold != null && scaffold.isDrawerOpen) {
        scaffold.closeDrawer();
      }
      context.go('/sessions/${created.id}');
    } on Object catch (e) {
      messenger.showSnackBar(
        SnackBar(content: Text(t.snackbarCreateSessionFailed('$e'))),
      );
    } finally {
      // 跳转后 sidebar 按钮仍挂载，需复位；welcome 按钮已随页面析构则跳过。
      if (mounted) setState(() => _creating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    return FilledButton.icon(
      key: widget.buttonKey,
      onPressed: _creating ? null : _onCreate,
      icon: _creating
          ? const SizedBox(
              width: 18,
              height: 18,
              child: CircularProgressIndicator(strokeWidth: 2),
            )
          : const Icon(Icons.add),
      label: Text(t.sidebarNewSession),
    );
  }
}
