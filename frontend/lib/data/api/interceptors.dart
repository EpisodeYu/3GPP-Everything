import 'package:dio/dio.dart';

import '../storage/token_store.dart';

typedef RefreshCallback = Future<String?> Function();
typedef RetryCallback = Future<Response<dynamic>> Function(RequestOptions options);

/// 给所有受保护请求加上 `Authorization: Bearer <access>`；
/// 收到 401 时调一次 [onRefresh]，成功就用新 access 重放；失败 → [onAuthLost]。
///
/// 重放标志通过 `RequestOptions.extra['retried']` 注入，避免无限循环。
class AuthInterceptor extends Interceptor {
  AuthInterceptor({
    required this.tokenStore,
    required this.onRefresh,
    required this.onAuthLost,
    required this.retry,
  });

  final TokenStore tokenStore;
  final RefreshCallback onRefresh;
  final void Function() onAuthLost;
  final RetryCallback retry;

  static const _retriedKey = 'retried';

  @override
  Future<void> onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    final access = await tokenStore.readAccess();
    if (access != null && access.isNotEmpty) {
      options.headers['Authorization'] = 'Bearer $access';
    }
    handler.next(options);
  }

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final shouldRetry = err.response?.statusCode == 401 &&
        err.requestOptions.extra[_retriedKey] != true;
    if (!shouldRetry) {
      return handler.next(err);
    }

    final newAccess = await onRefresh();
    if (newAccess == null) {
      onAuthLost();
      return handler.next(err);
    }

    final retryOptions = err.requestOptions
      ..headers['Authorization'] = 'Bearer $newAccess'
      ..extra[_retriedKey] = true;
    try {
      final response = await retry(retryOptions);
      handler.resolve(response);
    } on DioException catch (e) {
      handler.next(e);
    }
  }
}
