import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `MeResponse` 对齐，详见 `backend/app/schemas/auth.py`。
class Me {
  const Me({
    required this.id,
    required this.username,
    required this.role,
    required this.isActive,
    required this.createdAt,
    this.lastLoginAt,
  });

  factory Me.fromJson(Map<String, dynamic> json) => Me(
        id: json['id'] as String,
        username: json['username'] as String,
        role: json['role'] as String,
        isActive: json['is_active'] as bool,
        createdAt: DateTime.parse(json['created_at'] as String),
        lastLoginAt: json['last_login_at'] == null
            ? null
            : DateTime.parse(json['last_login_at'] as String),
      );

  final String id;
  final String username;
  final String role;
  final bool isActive;
  final DateTime createdAt;
  final DateTime? lastLoginAt;
}

/// 与后端 `TokenPair` 对齐。
class TokenPair {
  const TokenPair({
    required this.accessToken,
    required this.refreshToken,
    required this.expiresIn,
    this.tokenType = 'bearer',
  });

  factory TokenPair.fromJson(Map<String, dynamic> json) => TokenPair(
        accessToken: json['access_token'] as String,
        refreshToken: json['refresh_token'] as String,
        expiresIn: json['expires_in'] as int,
        tokenType: (json['token_type'] as String?) ?? 'bearer',
      );

  final String accessToken;
  final String refreshToken;
  final int expiresIn;

  /// 后端 `TokenPair.token_type` Literal["bearer"]；带在 Bearer header 前。
  final String tokenType;
}

/// 把后端 `ErrorOut`（详见 `backend/app/core/errors.py`）映射为一个轻量异常。
class AuthException implements Exception {
  AuthException(this.code, this.message);
  final String code;
  final String message;

  @override
  String toString() => 'AuthException($code): $message';
}

/// 受保护接口走带 AuthInterceptor 的 [dio]，
/// bootstrap / login / refresh 走裸 [dioPublic]，避免 401 自循环。
class AuthApi {
  AuthApi({required this.dio, required this.dioPublic});
  final Dio dio;
  final Dio dioPublic;

  Future<Me> bootstrapAdmin({
    required String username,
    required String password,
    required String inviteCode,
  }) async {
    try {
      final resp = await dioPublic.post<Map<String, dynamic>>(
        '/auth/bootstrap-admin',
        data: {
          'username': username,
          'password': password,
          'invite_code': inviteCode,
        },
      );
      return Me.fromJson(resp.data!);
    } on DioException catch (e) {
      throw _toAuthException(e);
    }
  }

  Future<TokenPair> login({
    required String username,
    required String password,
  }) async {
    try {
      final resp = await dioPublic.post<Map<String, dynamic>>(
        '/auth/login',
        data: {'username': username, 'password': password},
      );
      return TokenPair.fromJson(resp.data!);
    } on DioException catch (e) {
      throw _toAuthException(e);
    }
  }

  Future<TokenPair> refresh(String refreshToken) async {
    final resp = await dioPublic.post<Map<String, dynamic>>(
      '/auth/refresh',
      data: {'refresh_token': refreshToken},
    );
    return TokenPair.fromJson(resp.data!);
  }

  Future<void> logout(String refreshToken) async {
    await dio.post<void>(
      '/auth/logout',
      data: {'refresh_token': refreshToken},
    );
  }

  Future<Me> me() async {
    final resp = await dio.get<Map<String, dynamic>>('/auth/me');
    return Me.fromJson(resp.data!);
  }

  /// users 表是否为空（决定登录页是否显示"创建管理员"面板）。无需鉴权，走 public dio。
  Future<bool> bootstrapStatus() async {
    final resp = await dioPublic.get<Map<String, dynamic>>('/auth/bootstrap-status');
    return (resp.data?['needs_bootstrap'] as bool?) ?? false;
  }

  AuthException _toAuthException(DioException e) {
    final data = e.response?.data;
    if (data is Map<String, dynamic>) {
      final code = data['code']?.toString() ?? 'unknown';
      final msg = data['message']?.toString() ?? e.message ?? '请求失败';
      return AuthException(code, msg);
    }
    return AuthException('network_error', e.message ?? '网络异常');
  }
}

final authApiProvider = Provider<AuthApi>((ref) {
  return AuthApi(
    dio: ref.watch(dioProvider),
    dioPublic: ref.watch(dioPublicProvider),
  );
});

/// 登录页用：是否需要 bootstrap（users 表为空）。取不到状态时默认 false（隐藏面板）——
/// 已初始化的部署（绝大多数访客场景）稳妥地不显示死入口。
final bootstrapStatusProvider = FutureProvider<bool>((ref) async {
  try {
    return await ref.watch(authApiProvider).bootstrapStatus();
  } on Object {
    return false;
  }
});
