import 'package:tgpp/data/api/admin_api.dart';

/// 内存版 AdminApi，用于 dashboard widget 测。
///
/// 控制：
/// - [stats] / [tasks] 直接读
/// - [statsErr] / [tasksErr] / [rebuildErr] 注入 error
/// - [rebuildResult] 重建返回；记 [lastRebuildSpec] / [lastRebuildForce]
/// - [listTasksCalls] / [getStatsCalls] 计数器（轮询验证用）
class FakeAdminApi implements AdminApi {
  FakeAdminApi({
    StatsOut? stats,
    List<TaskOut> tasks = const [],
    TaskOut? rebuildResult,
  })  : _stats = stats ?? _emptyStats(),
        _tasks = List.of(tasks),
        _rebuildResult = rebuildResult ?? _emptyTask();

  StatsOut _stats;
  final List<TaskOut> _tasks;
  TaskOut _rebuildResult;

  Object? statsErr;
  Object? tasksErr;
  Object? rebuildErr;

  String? lastTaskFilter;
  int listTasksCalls = 0;
  int getStatsCalls = 0;

  String? lastRebuildSpec;
  bool? lastRebuildForce;

  void setStats(StatsOut s) => _stats = s;
  void setTasks(List<TaskOut> ts) {
    _tasks
      ..clear()
      ..addAll(ts);
  }

  void setRebuildResult(TaskOut t) => _rebuildResult = t;

  @override
  Future<StatsOut> getStats() async {
    getStatsCalls += 1;
    if (statsErr != null) throw statsErr!;
    return _stats;
  }

  @override
  Future<TaskListResponse> listTasks({
    String? statusFilter,
    int page = 1,
    int pageSize = 50,
  }) async {
    listTasksCalls += 1;
    lastTaskFilter = statusFilter;
    if (tasksErr != null) throw tasksErr!;
    final filtered = statusFilter == null
        ? List<TaskOut>.from(_tasks)
        : _tasks.where((t) => t.status == statusFilter).toList();
    return TaskListResponse(items: filtered, total: filtered.length);
  }

  @override
  Future<TaskOut> getTask(String taskId) async {
    final t = _tasks.firstWhere(
      (e) => e.id == taskId,
      orElse: () => throw StateError('FakeAdminApi: task not found $taskId'),
    );
    return t;
  }

  @override
  Future<TaskOut> triggerIndexRebuild({
    String? specId,
    bool force = false,
  }) async {
    lastRebuildSpec = specId;
    lastRebuildForce = force;
    if (rebuildErr != null) throw rebuildErr!;
    return _rebuildResult;
  }
}

StatsOut _emptyStats() => const StatsOut(
      documents: 0,
      chunks: 0,
      users: 0,
      sessions: 0,
      messages: 0,
      tasks: {},
      apiUsage7d: ApiUsage7dOut(
        llmInputTokens: 0,
        llmOutputTokens: 0,
        embeddingTokens: 0,
        rerankCalls: 0,
        webSearchCalls: 0,
        totalCostUsd: 0.0,
      ),
    );

TaskOut _emptyTask() => const TaskOut(
      id: 't-stub',
      kind: 'index_rebuild',
      status: 'queued',
      progress: 0,
      logTail: '',
      createdAt: '2026-05-25T00:00:00Z',
    );

StatsOut buildStats({
  int documents = 1270,
  int chunks = 394859,
  int users = 3,
  int sessions = 5,
  int messages = 12,
  Map<String, int> tasks = const {'queued': 1, 'running': 0, 'done': 4, 'failed': 0},
  ApiUsage7dOut? usage,
}) =>
    StatsOut(
      documents: documents,
      chunks: chunks,
      users: users,
      sessions: sessions,
      messages: messages,
      tasks: tasks,
      apiUsage7d: usage ??
          const ApiUsage7dOut(
            llmInputTokens: 1000,
            llmOutputTokens: 500,
            embeddingTokens: 200,
            rerankCalls: 7,
            webSearchCalls: 1,
            totalCostUsd: 0.0521,
          ),
    );

TaskOut buildTask({
  required String id,
  String kind = 'index_rebuild',
  String status = 'queued',
  int progress = 0,
  String logTail = '',
  Map<String, dynamic> payload = const {},
}) =>
    TaskOut(
      id: id,
      kind: kind,
      status: status,
      progress: progress,
      logTail: logTail,
      createdAt: '2026-05-25T10:00:00Z',
      payload: payload,
    );
