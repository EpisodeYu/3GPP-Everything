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

/// 带 AuthInterceptor 的 dio，所有受保护接口都走它。
final dioProvider = Provider<Dio>((ref) {
  final dio = Dio(_baseOptions());
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

  dio.interceptors.add(
    AuthInterceptor(
      tokenStore: tokenStore,
      onRefresh: refreshFn,
      onAuthLost: onAuthLost,
      retry: dio.fetch,
    ),
  );
  return dio;
});
