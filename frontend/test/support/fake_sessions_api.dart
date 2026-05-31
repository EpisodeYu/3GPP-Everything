import 'dart:async';

import 'package:tgpp/data/api/sessions_api.dart';

/// 内存版 SessionsApi，用于 controller 单测与 AppShell widget test。
///
/// - `list` 返回当前 `items`
/// - `create` push 到列表头
/// - `patch` 改 title / mode_default 后回写
/// - `delete` 从列表移除
/// - 任一方法在 fail 模式下抛 [SessionsApiFakeError]
class FakeSessionsApi implements SessionsApi {
  FakeSessionsApi({List<SessionOut> initial = const []})
      : items = List.of(initial);

  final List<SessionOut> items;
  bool failNext = false;

  /// 设了就让 `create` 在 `createCalls++` 之后挂起，直到测试 complete 它。
  /// 用于模拟网络延迟、验证 in-flight 期间按钮的连点防护。
  Completer<void>? createGate;
  int createCalls = 0;
  int patchCalls = 0;
  int deleteCalls = 0;
  int deleteAllCalls = 0;
  int listCalls = 0;

  void _maybeThrow(String op) {
    if (failNext) {
      failNext = false;
      throw SessionsApiFakeError(op);
    }
  }

  @override
  Future<SessionListResponse> list({int page = 1, int pageSize = 200}) async {
    listCalls += 1;
    _maybeThrow('list');
    return SessionListResponse(items: List.of(items), total: items.length);
  }

  @override
  Future<SessionOut> create({
    String title = '',
    String modeDefault = 'qa',
  }) async {
    createCalls += 1;
    _maybeThrow('create');
    if (createGate != null) await createGate!.future;
    final now = DateTime.utc(2026, 5, 24, 16, createCalls);
    final created = SessionOut(
      id: 'fake-${createCalls.toString().padLeft(3, '0')}',
      userId: 'user-1',
      title: title,
      modeDefault: modeDefault,
      status: 'active',
      createdAt: now,
      updatedAt: now,
    );
    items.insert(0, created);
    return created;
  }

  @override
  Future<SessionOut> get(String sid) async {
    return items.firstWhere((s) => s.id == sid);
  }

  @override
  Future<SessionOut> patch(
    String sid, {
    String? title,
    String? modeDefault,
  }) async {
    patchCalls += 1;
    _maybeThrow('patch');
    final idx = items.indexWhere((s) => s.id == sid);
    if (idx < 0) throw StateError('session not found: $sid');
    final cur = items[idx];
    final updated = SessionOut(
      id: cur.id,
      userId: cur.userId,
      title: title ?? cur.title,
      modeDefault: modeDefault ?? cur.modeDefault,
      status: cur.status,
      forkedFromSessionId: cur.forkedFromSessionId,
      forkedFromCheckpointId: cur.forkedFromCheckpointId,
      lastMessageAt: cur.lastMessageAt,
      createdAt: cur.createdAt,
      updatedAt: DateTime.utc(2026, 5, 24, 17, patchCalls),
    );
    items[idx] = updated;
    return updated;
  }

  @override
  Future<void> delete(String sid) async {
    deleteCalls += 1;
    _maybeThrow('delete');
    items.removeWhere((s) => s.id == sid);
  }

  @override
  Future<int> deleteAll() async {
    deleteAllCalls += 1;
    _maybeThrow('deleteAll');
    final n = items.length;
    items.clear();
    return n;
  }
}

class SessionsApiFakeError implements Exception {
  SessionsApiFakeError(this.op);
  final String op;
  @override
  String toString() => 'SessionsApiFakeError($op)';
}

SessionOut buildSession({
  required String id,
  String title = '',
  String status = 'active',
  String modeDefault = 'qa',
  String? forkedFromSessionId,
}) {
  final now = DateTime.utc(2026, 5, 24, 12);
  return SessionOut(
    id: id,
    userId: 'user-1',
    title: title,
    modeDefault: modeDefault,
    status: status,
    forkedFromSessionId: forkedFromSessionId,
    createdAt: now,
    updatedAt: now,
  );
}
