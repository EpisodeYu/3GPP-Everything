import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/auth_api.dart';
import '../../data/storage/token_store.dart';
import 'auth_state.dart';

class AuthController extends AsyncNotifier<AuthState> {
  late final AuthApi _api = ref.read(authApiProvider);
  late final TokenStore _store = ref.read(tokenStoreProvider);

  @override
  Future<AuthState> build() async {
    final access = await _store.readAccess();
    if (access == null || access.isEmpty) {
      return const AuthAnonymous();
    }
    // 401 时 dio 拦截器会做一次 refresh + retry；仍失败抛 DioException。
    try {
      final me = await _api.me();
      return AuthAuthenticated(me);
    } on Object {
      await _store.clear();
      return const AuthAnonymous();
    }
  }

  Future<void> login({
    required String username,
    required String password,
  }) async {
    state = const AsyncLoading<AuthState>();
    try {
      final pair = await _api.login(username: username, password: password);
      await _store.write(
        access: pair.accessToken,
        refresh: pair.refreshToken,
      );
      final me = await _api.me();
      state = AsyncData(AuthAuthenticated(me));
    } on AuthException catch (e) {
      state = AsyncData(AuthAnonymous(errorMessage: e.message));
    } on Object catch (e) {
      state = AsyncData(AuthAnonymous(errorMessage: '登录失败：$e'));
    }
  }

  Future<void> bootstrapAdmin({
    required String username,
    required String password,
    required String inviteCode,
  }) async {
    state = const AsyncLoading<AuthState>();
    try {
      await _api.bootstrapAdmin(
        username: username,
        password: password,
        inviteCode: inviteCode,
      );
      // 创建管理员只返回 MeResponse，不签发 token；接着自动 login。
      await login(username: username, password: password);
    } on AuthException catch (e) {
      state = AsyncData(AuthAnonymous(errorMessage: e.message));
    } on Object catch (e) {
      state = AsyncData(AuthAnonymous(errorMessage: '初始化失败：$e'));
    }
  }

  Future<void> logout() async {
    final refresh = await _store.readRefresh();
    if (refresh != null && refresh.isNotEmpty) {
      try {
        await _api.logout(refresh);
      } on Object {
        // 服务端拒绝也无所谓，本地清掉即可
      }
    }
    await _store.clear();
    state = const AsyncData(AuthAnonymous());
  }

  /// dio 401→refresh 失败时调用，把状态切到 anonymous，让路由 redirect 到 /login。
  void markLoggedOut() {
    state = const AsyncData(AuthAnonymous());
  }
}

final authControllerProvider =
    AsyncNotifierProvider<AuthController, AuthState>(AuthController.new);
