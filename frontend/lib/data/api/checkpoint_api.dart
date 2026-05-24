import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';
import 'messages_api.dart';
import 'sessions_api.dart';
import 'sse_client.dart';

/// 与后端 `PauseResponse`（`backend/app/schemas/checkpoint.py`）对齐。
class PauseResponse {
  const PauseResponse({
    required this.runId,
    required this.sessionId,
    required this.status,
  });

  factory PauseResponse.fromJson(Map<String, dynamic> j) => PauseResponse(
        runId: (j['run_id'] as String?) ?? '',
        sessionId: (j['session_id'] as String?) ?? '',
        status: (j['status'] as String?) ?? 'paused',
      );

  final String runId;
  final String sessionId;
  final String status;
}

/// 单条 checkpoint 概览。
class CheckpointOut {
  const CheckpointOut({
    required this.checkpointId,
    this.parentCheckpointId,
    this.createdAt = '',
    this.nextNodes = const [],
    this.lastNode,
  });

  factory CheckpointOut.fromJson(Map<String, dynamic> j) => CheckpointOut(
        checkpointId: (j['checkpoint_id'] as String?) ?? '',
        parentCheckpointId: j['parent_checkpoint_id'] as String?,
        createdAt: (j['created_at'] as String?) ?? '',
        nextNodes: ((j['next_nodes'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(),
        lastNode: j['last_node'] as String?,
      );

  final String checkpointId;
  final String? parentCheckpointId;
  final String createdAt;
  final List<String> nextNodes;
  final String? lastNode;
}

class CheckpointListResponse {
  const CheckpointListResponse({required this.items});

  factory CheckpointListResponse.fromJson(Map<String, dynamic> j) =>
      CheckpointListResponse(
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(CheckpointOut.fromJson)
            .toList(),
      );

  final List<CheckpointOut> items;
}

class ForkResponse {
  const ForkResponse({required this.newSession});

  factory ForkResponse.fromJson(Map<String, dynamic> j) => ForkResponse(
        newSession: SessionOut.fromJson(
          (j['new_session'] as Map).cast<String, dynamic>(),
        ),
      );

  final SessionOut newSession;
}

class RollbackResponse {
  const RollbackResponse({
    required this.deletedMessages,
    this.headCheckpointId,
  });

  factory RollbackResponse.fromJson(Map<String, dynamic> j) => RollbackResponse(
        deletedMessages: (j['deleted_messages'] as num?)?.toInt() ?? 0,
        headCheckpointId: j['head_checkpoint_id'] as String?,
      );

  final int deletedMessages;
  final String? headCheckpointId;
}

/// Checkpoint 5 路由的薄包装（M4.8 + M5.4）。
///
/// 协议锚：[`backend/app/api/v1/checkpoint.py`](../../../../../backend/app/api/v1/checkpoint.py) +
/// `docs/03-development/04-backend-api.md` §2 Checkpoint 节。
///
/// 注意：`resume` 返回 SSE 流（与 `MessagesApi.sendMessage` 同款），调用方需要
/// 自行 listen + cancelToken；不传 cancelToken 也能跑，但失去取消能力。
class CheckpointApi {
  CheckpointApi(this._dio);

  final Dio _dio;

  /// POST `/sessions/{sid}/runs/{rid}/pause`。
  Future<PauseResponse> pause(String sid, String runId) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/sessions/$sid/runs/$runId/pause',
    );
    return PauseResponse.fromJson(resp.data!);
  }

  /// POST `/sessions/{sid}/resume` —— 返回 SSE 事件流，与 sendMessage 同款。
  Stream<ChatEvent> resume(
    String sid, {
    CancelToken? cancelToken,
  }) async* {
    final resp = await _dio.post<ResponseBody>(
      '/sessions/$sid/resume',
      options: Options(
        responseType: ResponseType.stream,
        headers: {'Accept': 'text/event-stream'},
        // 续跑同样可能耗时，关掉 receive timeout
        receiveTimeout: Duration.zero,
      ),
      cancelToken: cancelToken,
    );
    await for (final frame in sseFramesFromBytes(resp.data!.stream)) {
      yield ChatEvent.fromFrame(frame);
    }
  }

  /// GET `/sessions/{sid}/checkpoints` —— 列出该会话所有 checkpoint。
  Future<CheckpointListResponse> list(String sid) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/sessions/$sid/checkpoints',
    );
    return CheckpointListResponse.fromJson(resp.data!);
  }

  /// POST `/sessions/{sid}/fork`。body `{checkpoint_id, new_user_message?, title?}`。
  Future<ForkResponse> fork(
    String sid, {
    required String checkpointId,
    String? newUserMessage,
    String? title,
  }) async {
    final body = <String, dynamic>{
      'checkpoint_id': checkpointId,
      'new_user_message': ?newUserMessage,
      'title': ?title,
    };
    final resp = await _dio.post<Map<String, dynamic>>(
      '/sessions/$sid/fork',
      data: body,
    );
    return ForkResponse.fromJson(resp.data!);
  }

  /// POST `/sessions/{sid}/rollback`。body `{last_n}`。
  Future<RollbackResponse> rollback(String sid, {required int lastN}) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/sessions/$sid/rollback',
      data: {'last_n': lastN},
    );
    return RollbackResponse.fromJson(resp.data!);
  }
}

final checkpointApiProvider =
    Provider<CheckpointApi>((ref) => CheckpointApi(ref.watch(dioProvider)));
