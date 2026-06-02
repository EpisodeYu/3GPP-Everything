import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/checkpoint_api.dart';
import '../../data/api/sessions_api.dart';
import '../auth/auth_controller.dart';
import '../auth/auth_state.dart';

/// 管理当前用户的会话列表。
///
/// 设计取舍：
/// - 单一 `AsyncNotifier<List<SessionOut>>`，不再拆 "list" / "current" provider，
///   sidebar 与 chat page 各自从同一份数据读，简单一致。
/// - 写操作（create / rename / delete）成功后做**局部乐观更新**而非全量 refresh，
///   避免 sidebar 闪烁；失败时回滚到上一次成功状态。
class SessionsController extends AsyncNotifier<List<SessionOut>> {
  late final SessionsApi _api = ref.read(sessionsApiProvider);

  /// "空草稿会话" 的 id 集合：由 [createBlank] 新建、用户还没发过任何消息。
  ///
  /// 用途（"新会话即草稿，发首条消息前不落地"语义）：
  /// - [isDraft]：sidebar "新会话" 按钮判断当前是否已在空草稿里 → 复用而非再建一个；
  /// - [discardDraft]：用户离开空草稿（切会话 / 退出）时把它丢掉，不留一堆空会话；
  /// - [markUsed]：首条消息发出后移出草稿集，从此不再被自动丢弃。
  ///
  /// 只跟踪"本端这次 createBlank 出来的"会话 → 删除前可确信它在服务端确为空，
  /// 不会误删有内容的会话（刷新页面后草稿集清空，旧空会话不会被自动删，偏保守）。
  final Set<String> _draftSessionIds = <String>{};

  @override
  Future<List<SessionOut>> build() async {
    // 必须等待鉴权状态恢复（从本地存储读取 token 并验证）。
    // 否则在首屏加载时，SessionsController 可能在 AuthController 完成前
    // 就发起请求，导致 401 错误。
    final authState = await ref.watch(authControllerProvider.future);
    if (authState is! AuthAuthenticated) {
      return const [];
    }

    final resp = await _api.list();
    return resp.items;
  }

  /// 强制重新拉取（用于下拉刷新或外部状态变化后保险同步）。
  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(() async => (await _api.list()).items);
  }

  /// 创建空会话并返回新会话；成功后插入列表头部。
  Future<SessionOut> createBlank({
    String title = '',
    String modeDefault = 'qa',
  }) async {
    final created = await _api.create(title: title, modeDefault: modeDefault);
    _draftSessionIds.add(created.id);
    final prev = state.value ?? const <SessionOut>[];
    state = AsyncData([created, ...prev]);
    return created;
  }

  /// 该会话是否仍是"空草稿"（createBlank 出来、还没发过消息）。
  bool isDraft(String sid) => _draftSessionIds.contains(sid);

  /// 首条消息发出后调用：把会话移出草稿集，从此不再被 [discardDraft] 丢弃。
  void markUsed(String sid) {
    _draftSessionIds.remove(sid);
  }

  /// 离开空草稿会话时丢弃它：仅当 [sid] 仍在草稿集（= 确为本端新建且没发过消息）。
  ///
  /// 乐观从列表移除并 `DELETE`；删除失败回滚列表，保持与后端一致。非草稿一律 no-op，
  /// 避免误删有内容的会话。
  Future<void> discardDraft(String sid) async {
    if (!_draftSessionIds.remove(sid)) return;
    final prev = state.value ?? const <SessionOut>[];
    if (!prev.any((s) => s.id == sid)) return;
    state = AsyncData([for (final s in prev) if (s.id != sid) s]);
    try {
      await _api.delete(sid);
    } on Object {
      state = AsyncData(prev);
    }
  }

  /// 重命名 / 改默认模式。失败时回滚。
  Future<void> rename(String sid, String newTitle) async {
    final prev = state.value ?? const <SessionOut>[];
    try {
      final updated = await _api.patch(sid, title: newTitle);
      state = AsyncData([
        for (final s in prev) if (s.id == sid) updated else s,
      ]);
    } on Object {
      state = AsyncData(prev);
      rethrow;
    }
  }

  /// 后端首轮自动标题：通过 chat SSE `title` 事件推下来，仅更新本地列表，不再 PATCH
  /// （后端已落库）。会话不在列表里则 no-op。
  void applyTitle(String sid, String title) {
    if (title.trim().isEmpty) return;
    final prev = state.value;
    if (prev == null) return;
    state = AsyncData([
      for (final s in prev) if (s.id == sid) _withTitle(s, title) else s,
    ]);
  }

  /// 删除单个会话。失败时回滚。
  Future<void> delete(String sid) async {
    _draftSessionIds.remove(sid);
    final prev = state.value ?? const <SessionOut>[];
    state = AsyncData([for (final s in prev) if (s.id != sid) s]);
    try {
      await _api.delete(sid);
    } on Object {
      state = AsyncData(prev);
      rethrow;
    }
  }

  /// 一键清空当前用户所有会话。成功后列表置空；失败时回滚到调用前状态。
  ///
  /// 返回后端真实删除数（用于 snackbar 回显）。前端乐观清空与后端结果独立：
  /// 即使后端返回 0（无数据可删），UI 仍按"清空成功"反馈，不让用户困惑。
  Future<int> deleteAll() async {
    _draftSessionIds.clear();
    final prev = state.value ?? const <SessionOut>[];
    state = const AsyncData(<SessionOut>[]);
    try {
      return await _api.deleteAll();
    } on Object {
      state = AsyncData(prev);
      rethrow;
    }
  }

  /// 从指定 checkpoint 分叉出新会话（M5.4）。成功后：
  /// - 新会话插到列表头
  /// - 旧 sid 保持原状态（2026-06-01 行为变更：fork 不再 archive 原会话，
  ///   用户可以继续在原会话提问；与后端 `fork_session` 行为对齐）
  ///
  /// 失败时不修改列表，[CheckpointApi.fork] 的异常向上抛由调用方提示。
  Future<SessionOut> fork({
    required String sid,
    required String checkpointId,
    String? newUserMessage,
    String? title,
  }) async {
    final api = ref.read(checkpointApiProvider);
    final resp = await api.fork(
      sid,
      checkpointId: checkpointId,
      newUserMessage: newUserMessage,
      title: title,
    );
    final prev = state.value ?? const <SessionOut>[];
    state = AsyncData([resp.newSession, ...prev]);
    return resp.newSession;
  }

  /// 局部 SessionOut copy（仅改 title 字段）。
  SessionOut _withTitle(SessionOut s, String title) => SessionOut(
        id: s.id,
        userId: s.userId,
        title: title,
        modeDefault: s.modeDefault,
        status: s.status,
        forkedFromSessionId: s.forkedFromSessionId,
        forkedFromCheckpointId: s.forkedFromCheckpointId,
        lastMessageAt: s.lastMessageAt,
        createdAt: s.createdAt,
        updatedAt: s.updatedAt,
      );
}

final sessionsControllerProvider =
    AsyncNotifierProvider<SessionsController, List<SessionOut>>(
        SessionsController.new);
