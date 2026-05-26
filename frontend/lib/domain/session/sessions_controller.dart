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
    final prev = state.value ?? const <SessionOut>[];
    state = AsyncData([created, ...prev]);
    return created;
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

  /// 删除单个会话。失败时回滚。
  Future<void> delete(String sid) async {
    final prev = state.value ?? const <SessionOut>[];
    state = AsyncData([for (final s in prev) if (s.id != sid) s]);
    try {
      await _api.delete(sid);
    } on Object {
      state = AsyncData(prev);
      rethrow;
    }
  }

  /// 从指定 checkpoint 分叉出新会话（M5.4）。成功后：
  /// - 旧 sid 在列表里 status 改成 `archived_branch`（视觉灰度 / "分叉历史" 分组）
  /// - 新会话插到列表头
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
    state = AsyncData([
      resp.newSession,
      for (final s in prev)
        if (s.id == sid) _withStatus(s, 'archived_branch') else s,
    ]);
    return resp.newSession;
  }

  /// 局部 SessionOut copy（仅改 status 字段，避免给 SessionOut 加 copyWith 触发面更广的改动）。
  SessionOut _withStatus(SessionOut s, String status) => SessionOut(
        id: s.id,
        userId: s.userId,
        title: s.title,
        modeDefault: s.modeDefault,
        status: status,
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
