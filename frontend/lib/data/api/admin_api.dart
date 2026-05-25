import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `ApiUsage7dOut`（`backend/app/schemas/admin.py`）对齐。
class ApiUsage7dOut {
  const ApiUsage7dOut({
    required this.llmInputTokens,
    required this.llmOutputTokens,
    required this.embeddingTokens,
    required this.rerankCalls,
    required this.webSearchCalls,
    required this.totalCostUsd,
  });

  factory ApiUsage7dOut.fromJson(Map<String, dynamic> j) => ApiUsage7dOut(
        llmInputTokens: (j['llm_input_tokens'] as num?)?.toInt() ?? 0,
        llmOutputTokens: (j['llm_output_tokens'] as num?)?.toInt() ?? 0,
        embeddingTokens: (j['embedding_tokens'] as num?)?.toInt() ?? 0,
        rerankCalls: (j['rerank_calls'] as num?)?.toInt() ?? 0,
        webSearchCalls: (j['web_search_calls'] as num?)?.toInt() ?? 0,
        totalCostUsd: (j['total_cost_usd'] as num?)?.toDouble() ?? 0.0,
      );

  final int llmInputTokens;
  final int llmOutputTokens;
  final int embeddingTokens;
  final int rerankCalls;
  final int webSearchCalls;
  final double totalCostUsd;
}

/// 与后端 `StatsOut` 对齐。
class StatsOut {
  const StatsOut({
    required this.documents,
    required this.chunks,
    required this.users,
    required this.sessions,
    required this.messages,
    required this.tasks,
    required this.apiUsage7d,
  });

  factory StatsOut.fromJson(Map<String, dynamic> j) => StatsOut(
        documents: (j['documents'] as num?)?.toInt() ?? 0,
        chunks: (j['chunks'] as num?)?.toInt() ?? 0,
        users: (j['users'] as num?)?.toInt() ?? 0,
        sessions: (j['sessions'] as num?)?.toInt() ?? 0,
        messages: (j['messages'] as num?)?.toInt() ?? 0,
        tasks: ((j['tasks'] as Map?) ?? const {})
            .map((k, v) => MapEntry(k.toString(), (v as num).toInt())),
        apiUsage7d: ApiUsage7dOut.fromJson(
          ((j['api_usage_7d'] as Map?) ?? const {}).cast<String, dynamic>(),
        ),
      );

  final int documents;
  final int chunks;
  final int users;
  final int sessions;
  final int messages;
  final Map<String, int> tasks;
  final ApiUsage7dOut apiUsage7d;
}

/// 与后端 `TaskOut` 对齐。
///
/// `payload` 透传后端 JSON（`index_rebuild` 时含 `spec_id`/`force`）。
class TaskOut {
  const TaskOut({
    required this.id,
    required this.kind,
    required this.status,
    required this.progress,
    required this.logTail,
    required this.createdAt,
    this.payload = const {},
    this.startedAt,
    this.finishedAt,
    this.createdBy,
  });

  factory TaskOut.fromJson(Map<String, dynamic> j) => TaskOut(
        id: (j['id'] as String?) ?? '',
        kind: (j['kind'] as String?) ?? '',
        status: (j['status'] as String?) ?? '',
        progress: (j['progress'] as num?)?.toInt() ?? 0,
        logTail: (j['log_tail'] as String?) ?? '',
        createdAt: (j['created_at'] as String?) ?? '',
        payload: (j['payload'] is Map)
            ? Map<String, dynamic>.from(j['payload'] as Map)
            : const {},
        startedAt: j['started_at'] as String?,
        finishedAt: j['finished_at'] as String?,
        createdBy: j['created_by'] as String?,
      );

  final String id;
  final String kind;
  final String status;
  final int progress;
  final String logTail;
  final String createdAt;
  final Map<String, dynamic> payload;
  final String? startedAt;
  final String? finishedAt;
  final String? createdBy;
}

class TaskListResponse {
  const TaskListResponse({required this.items, required this.total});

  factory TaskListResponse.fromJson(Map<String, dynamic> j) => TaskListResponse(
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(TaskOut.fromJson)
            .toList(),
        total: (j['total'] as num?)?.toInt() ?? 0,
      );

  final List<TaskOut> items;
  final int total;
}

/// `/admin/*` 4 路由的薄包装（M5.5）。
///
/// 协议锚：
/// [`backend/app/api/v1/admin.py`](../../../../../backend/app/api/v1/admin.py)
/// + `docs/03-development/04-backend-api.md §M4.10`。
///
/// 权限：所有路由后端要求 `role=admin`；非 admin 调用会拿 403。前端在
/// `core/router.dart` + sidebar 入口隐藏构成第一道防线，后端 403 是第二道。
class AdminApi {
  AdminApi(this._dio);

  final Dio _dio;

  /// GET `/admin/stats` — 索引/chunk/任务/用量聚合。
  Future<StatsOut> getStats() async {
    final resp = await _dio.get<Map<String, dynamic>>('/admin/stats');
    return StatsOut.fromJson(resp.data!);
  }

  /// GET `/admin/tasks?status=&page=&page_size=` — 任务列表。
  Future<TaskListResponse> listTasks({
    String? statusFilter,
    int page = 1,
    int pageSize = 50,
  }) async {
    final qp = <String, dynamic>{
      'page': page,
      'page_size': pageSize,
    };
    if (statusFilter != null && statusFilter.isNotEmpty) {
      qp['status'] = statusFilter;
    }
    final resp = await _dio.get<Map<String, dynamic>>(
      '/admin/tasks',
      queryParameters: qp,
    );
    return TaskListResponse.fromJson(resp.data!);
  }

  /// GET `/admin/tasks/{tid}` — 单任务详情（轮询用）。
  Future<TaskOut> getTask(String taskId) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/admin/tasks/$taskId',
    );
    return TaskOut.fromJson(resp.data!);
  }

  /// POST `/admin/index/rebuild` — 触发索引重建。
  ///
  /// `specId=null` 透传后端 = 全量重建。
  Future<TaskOut> triggerIndexRebuild({
    String? specId,
    bool force = false,
  }) async {
    final body = <String, dynamic>{
      'spec_id': specId,
      'force': force,
    };
    final resp = await _dio.post<Map<String, dynamic>>(
      '/admin/index/rebuild',
      data: body,
    );
    return TaskOut.fromJson(resp.data!);
  }
}

final adminApiProvider =
    Provider<AdminApi>((ref) => AdminApi(ref.watch(dioProvider)));
