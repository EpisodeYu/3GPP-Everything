import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../domain/auth/auth_controller.dart';
import '../domain/auth/auth_state.dart';
import '../features/auth/login_page.dart';
import '../features/chat/chat_page.dart';

const _publicRoutes = <String>{'/login'};

/// 监听 Riverpod 的 authState，触发 GoRouter 重新评估 redirect。
class _AuthRefreshNotifier extends ChangeNotifier {
  _AuthRefreshNotifier(this._ref) {
    _ref.listen<AsyncValue<AuthState>>(
      authControllerProvider,
      (_, _) => notifyListeners(),
    );
  }

  // 持有 ref 仅为保活 listen 订阅，本类不会主动销毁。
  // ignore: unused_field
  final Ref _ref;
}

final routerProvider = Provider<GoRouter>((ref) {
  final refreshNotifier = _AuthRefreshNotifier(ref);

  return GoRouter(
    initialLocation: '/chat',
    refreshListenable: refreshNotifier,
    redirect: (context, state) {
      final auth = ref.read(authControllerProvider);
      // 鉴权状态未恢复完成 → 暂不跳转，停在当前页（首屏会是 splash 风格的空白）
      if (auth.isLoading || !auth.hasValue) return null;
      final value = auth.value;
      final loggedIn = value is AuthAuthenticated;
      final goingPublic = _publicRoutes.contains(state.matchedLocation);

      if (!loggedIn && !goingPublic) return '/login';
      if (loggedIn && goingPublic) return '/chat';
      return null;
    },
    routes: [
      GoRoute(path: '/login', builder: (_, _) => const LoginPage()),
      GoRoute(path: '/chat', builder: (_, _) => const ChatPage()),
    ],
  );
});
