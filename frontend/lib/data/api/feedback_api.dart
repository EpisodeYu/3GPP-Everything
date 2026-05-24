import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `FeedbackOut` 对齐（`backend/app/schemas/feedback.py`）。
///
/// 注意：thumb 取 `1` 或 `-1`（后端 `Literal[1, -1]`）。
class FeedbackOut {
  const FeedbackOut({
    required this.id,
    required this.messageId,
    required this.thumb,
    this.reason,
    required this.createdAt,
  });

  factory FeedbackOut.fromJson(Map<String, dynamic> j) => FeedbackOut(
        id: j['id'] as String,
        messageId: j['message_id'] as String,
        thumb: (j['thumb'] as num).toInt(),
        reason: j['reason'] as String?,
        createdAt: DateTime.parse(j['created_at'] as String),
      );

  final String id;
  final String messageId;
  final int thumb;
  final String? reason;
  final DateTime createdAt;
}

class FeedbackApi {
  FeedbackApi(this._dio);

  final Dio _dio;

  /// POST `/messages/{mid}/feedback`，upsert 语义（第二次提交覆盖前一条）。
  Future<FeedbackOut> upsert(
    String messageId, {
    required int thumb,
    String? reason,
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/messages/$messageId/feedback',
      data: {
        'thumb': thumb,
        if (reason != null && reason.isNotEmpty) 'reason': reason,
      },
    );
    return FeedbackOut.fromJson(resp.data!);
  }
}

final feedbackApiProvider =
    Provider<FeedbackApi>((ref) => FeedbackApi(ref.watch(dioProvider)));
