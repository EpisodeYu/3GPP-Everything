import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `NoteOut` 对齐（`backend/app/schemas/notes.py`）。
class NoteOut {
  const NoteOut({
    required this.id,
    required this.targetType,
    required this.targetId,
    required this.body,
    required this.createdAt,
    required this.updatedAt,
    this.sessionId,
    this.preview,
  });

  factory NoteOut.fromJson(Map<String, dynamic> j) => NoteOut(
        id: j['id'] as String,
        targetType: j['target_type'] as String,
        targetId: j['target_id'] as String,
        body: (j['body'] as String?) ?? '',
        createdAt: DateTime.parse(j['created_at'] as String),
        updatedAt: DateTime.parse(j['updated_at'] as String),
        sessionId: j['session_id'] as String?,
        preview: j['preview'] as String?,
      );

  final String id;
  final String targetType;
  final String targetId;
  final String body;
  final DateTime createdAt;
  final DateTime updatedAt;

  /// list 时后端 enrich：message target 所属会话 + 内容预览，用于跳回原消息。
  /// create/patch 返回 / chunk 类型 / target 已删 → null。
  final String? sessionId;
  final String? preview;
}

class NoteListResponse {
  const NoteListResponse({required this.items});

  factory NoteListResponse.fromJson(Map<String, dynamic> j) => NoteListResponse(
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(NoteOut.fromJson)
            .toList(),
      );

  final List<NoteOut> items;
}

class NotesApi {
  NotesApi(this._dio);

  final Dio _dio;

  Future<NoteOut> create({
    required String targetType,
    required String targetId,
    String body = '',
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/notes',
      data: {
        'target_type': targetType,
        'target_id': targetId,
        'body': body,
      },
    );
    return NoteOut.fromJson(resp.data!);
  }

  Future<NoteListResponse> list({String? targetType, String? targetId}) async {
    final qp = <String, dynamic>{};
    if (targetType != null && targetType.isNotEmpty) {
      qp['target_type'] = targetType;
    }
    if (targetId != null && targetId.isNotEmpty) {
      qp['target_id'] = targetId;
    }
    final resp = await _dio.get<Map<String, dynamic>>(
      '/notes',
      queryParameters: qp.isEmpty ? null : qp,
    );
    return NoteListResponse.fromJson(resp.data!);
  }

  Future<NoteOut> patch(String nid, {required String body}) async {
    final resp = await _dio.patch<Map<String, dynamic>>(
      '/notes/$nid',
      data: {'body': body},
    );
    return NoteOut.fromJson(resp.data!);
  }

  Future<void> delete(String nid) async {
    await _dio.delete<void>('/notes/$nid');
  }
}

final notesApiProvider =
    Provider<NotesApi>((ref) => NotesApi(ref.watch(dioProvider)));
