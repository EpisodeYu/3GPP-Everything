import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `FavoriteOut` 对齐（`backend/app/schemas/favorites.py`）。
class FavoriteOut {
  const FavoriteOut({
    required this.id,
    required this.targetType,
    required this.targetId,
    required this.createdAt,
  });

  factory FavoriteOut.fromJson(Map<String, dynamic> j) => FavoriteOut(
        id: j['id'] as String,
        targetType: j['target_type'] as String,
        targetId: j['target_id'] as String,
        createdAt: DateTime.parse(j['created_at'] as String),
      );

  final String id;

  /// `'chunk'` | `'message'`。
  final String targetType;
  final String targetId;
  final DateTime createdAt;
}

class FavoriteListResponse {
  const FavoriteListResponse({required this.items});

  factory FavoriteListResponse.fromJson(Map<String, dynamic> j) =>
      FavoriteListResponse(
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(FavoriteOut.fromJson)
            .toList(),
      );

  final List<FavoriteOut> items;
}

class FavoritesApi {
  FavoritesApi(this._dio);

  final Dio _dio;

  Future<FavoriteOut> create({
    required String targetType,
    required String targetId,
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/favorites',
      data: {'target_type': targetType, 'target_id': targetId},
    );
    return FavoriteOut.fromJson(resp.data!);
  }

  Future<FavoriteListResponse> list({String? targetType}) async {
    final qp = <String, dynamic>{};
    if (targetType != null && targetType.isNotEmpty) {
      qp['target_type'] = targetType;
    }
    final resp = await _dio.get<Map<String, dynamic>>(
      '/favorites',
      queryParameters: qp.isEmpty ? null : qp,
    );
    return FavoriteListResponse.fromJson(resp.data!);
  }

  Future<void> delete(String fid) async {
    await _dio.delete<void>('/favorites/$fid');
  }
}

final favoritesApiProvider =
    Provider<FavoritesApi>((ref) => FavoritesApi(ref.watch(dioProvider)));
