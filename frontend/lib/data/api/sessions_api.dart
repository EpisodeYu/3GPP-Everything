import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `SessionOut`（详见 `backend/app/schemas/sessions.py`）对齐。
class SessionOut {
  const SessionOut({
    required this.id,
    required this.userId,
    required this.title,
    required this.modeDefault,
    required this.status,
    required this.createdAt,
    required this.updatedAt,
    this.forkedFromSessionId,
    this.forkedFromCheckpointId,
    this.lastMessageAt,
  });

  factory SessionOut.fromJson(Map<String, dynamic> json) => SessionOut(
        id: json['id'] as String,
        userId: json['user_id'] as String,
        title: (json['title'] as String?) ?? '',
        modeDefault: json['mode_default'] as String,
        status: json['status'] as String,
        forkedFromSessionId: json['forked_from_session_id'] as String?,
        forkedFromCheckpointId: json['forked_from_checkpoint_id'] as String?,
        lastMessageAt: _parseDate(json['last_message_at']),
        createdAt: DateTime.parse(json['created_at'] as String),
        updatedAt: DateTime.parse(json['updated_at'] as String),
      );

  static DateTime? _parseDate(Object? v) =>
      v == null ? null : DateTime.parse(v as String);

  final String id;
  final String userId;
  final String title;

  /// 恒为 `"qa"`（raw_lookup 已下线），详见 `backend/app/schemas/sessions.py::Mode`。
  final String modeDefault;

  /// `"active"` | `"paused"` | `"archived_branch"`。
  final String status;
  final String? forkedFromSessionId;
  final String? forkedFromCheckpointId;
  final DateTime? lastMessageAt;
  final DateTime createdAt;
  final DateTime updatedAt;

  bool get isArchivedBranch => status == 'archived_branch';

  /// 列表展示用：优先 title；空 title fallback "新会话"。
  String get displayTitle => title.trim().isEmpty ? '新会话' : title;
}

class SessionListResponse {
  const SessionListResponse({required this.items, required this.total});

  factory SessionListResponse.fromJson(Map<String, dynamic> json) =>
      SessionListResponse(
        items: (json['items'] as List<dynamic>)
            .map((e) => SessionOut.fromJson(e as Map<String, dynamic>))
            .toList(),
        total: json['total'] as int,
      );

  final List<SessionOut> items;
  final int total;
}

class SessionsApi {
  SessionsApi(this._dio);
  final Dio _dio;

  /// 与后端 `GET /sessions?page=&page_size=` 对齐。
  /// M5.1：MVP 一次拉前 200 条，足够 vibe-coding 期 demo；翻页留给后续。
  Future<SessionListResponse> list({int page = 1, int pageSize = 200}) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/sessions',
      queryParameters: {'page': page, 'page_size': pageSize},
    );
    return SessionListResponse.fromJson(resp.data!);
  }

  Future<SessionOut> create({String title = '', String modeDefault = 'qa'}) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/sessions',
      data: {'title': title, 'mode_default': modeDefault},
    );
    return SessionOut.fromJson(resp.data!);
  }

  Future<SessionOut> get(String sid) async {
    final resp = await _dio.get<Map<String, dynamic>>('/sessions/$sid');
    return SessionOut.fromJson(resp.data!);
  }

  /// `title` / `modeDefault` 任意可空：null 表示该字段不改。
  Future<SessionOut> patch(
    String sid, {
    String? title,
    String? modeDefault,
  }) async {
    final body = <String, dynamic>{};
    if (title != null) body['title'] = title;
    if (modeDefault != null) body['mode_default'] = modeDefault;
    final resp = await _dio.patch<Map<String, dynamic>>(
      '/sessions/$sid',
      data: body,
    );
    return SessionOut.fromJson(resp.data!);
  }

  Future<void> delete(String sid) async {
    await _dio.delete<void>('/sessions/$sid');
  }

  /// 清空当前用户所有会话；后端 `DELETE /sessions`（M5.x 一键清空）。
  /// 返回真实删除数（即使前端乐观清空成功，也用此值回显 snackbar）。
  Future<int> deleteAll() async {
    final resp = await _dio.delete<Map<String, dynamic>>('/sessions');
    final data = resp.data;
    if (data == null) return 0;
    return (data['deleted'] as num?)?.toInt() ?? 0;
  }
}

final sessionsApiProvider =
    Provider<SessionsApi>((ref) => SessionsApi(ref.watch(dioProvider)));
