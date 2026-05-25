import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api_base.dart';
import '../../domain/auth/auth_controller.dart';
import '../storage/token_store.dart';
import 'interceptors.dart';

BaseOptions _baseOptions() => BaseOptions(
      baseUrl: ApiBase.url,
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 30),
      headers: {'Accept': 'application/json'},
      // 后端把业务错误塞 4xx，dio 默认 throw；
      // 我们在 interceptor / api 层统一捕获并归一化。
    );

/// "公开 dio"：不带 AuthInterceptor，用于 bootstrap / login / refresh，
/// 避免 refresh 自身 401 触发再次 refresh。
final dioPublicProvider = Provider<Dio>((ref) {
  return Dio(_baseOptions());
});

/// access token 刷新 + 登出的单一实现；dio [AuthInterceptor] 与 web SSE Fetch 路径
/// （绕过 interceptor，见 `sse_transport_web.dart`）共用，避免两份刷新逻辑漂移。
class TokenRefresher {
  const TokenRefresher({required this.refresh, required this.onAuthLost});

  /// 用 refresh token 换新 access；成功返回新 access，失败返回 null。
  final Future<String?> Function() refresh;

  /// refresh 失败：清 token + 把 auth controller 切到 anonymous。
  final void Function() onAuthLost;
}

final tokenRefresherProvider = Provider<TokenRefresher>((ref) {
  final tokenStore = ref.read(tokenStoreProvider);
  final publicDio = ref.read(dioPublicProvider);

  Future<String?> refreshFn() async {
    final refresh = await tokenStore.readRefresh();
    if (refresh == null || refresh.isEmpty) return null;
    try {
      final resp = await publicDio.post<Map<String, dynamic>>(
        '/auth/refresh',
        data: {'refresh_token': refresh},
      );
      final data = resp.data!;
      final access = data['access_token'] as String;
      final newRefresh = data['refresh_token'] as String;
      await tokenStore.write(access: access, refresh: newRefresh);
      return access;
    } on DioException {
      return null;
    }
  }

  void onAuthLost() {
    // controller 同步切到 anonymous；token 已在 refresh 失败那一刻无效，清掉。
    tokenStore.clear();
    ref.read(authControllerProvider.notifier).markLoggedOut();
  }

  return TokenRefresher(refresh: refreshFn, onAuthLost: onAuthLost);
});

/// 带 AuthInterceptor 的 dio，所有受保护接口都走它。
final dioProvider = Provider<Dio>((ref) {
  final dio = Dio(_baseOptions());
  final tokenStore = ref.read(tokenStoreProvider);
  final refresher = ref.read(tokenRefresherProvider);

  dio.interceptors.add(
    AuthInterceptor(
      tokenStore: tokenStore,
      onRefresh: refresher.refresh,
      onAuthLost: refresher.onAuthLost,
      retry: dio.fetch,
    ),
  );
  return dio;
});
