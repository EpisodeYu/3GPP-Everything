import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/sessions_api.dart';

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
}

final sessionsControllerProvider =
    AsyncNotifierProvider<SessionsController, List<SessionOut>>(
        SessionsController.new);
