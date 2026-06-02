import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../domain/auth/auth_controller.dart';
import '../domain/auth/auth_state.dart';
import '../features/admin/admin_dashboard.dart';
import '../features/auth/login_page.dart';
import '../features/chat/chat_page.dart';
import '../features/favorites/favorites_page.dart';
import '../features/notes/notes_page.dart';
import '../features/reader/reader_page.dart';
import '../features/shell/app_shell.dart';

const _publicRoutes = <String>{'/login'};
const _adminRoutes = <String>{'/admin'};

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
      // RBAC：非 admin 访问 /admin → 弹回 /chat。后端 403 是第二道防线。
      if (value is AuthAuthenticated &&
          _adminRoutes.contains(state.matchedLocation) &&
          value.me.role != 'admin') {
        return '/chat';
      }
      return null;
    },
    routes: [
      GoRoute(path: '/login', builder: (_, _) => const LoginPage()),
      ShellRoute(
        builder: (_, _, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: '/chat',
            builder: (_, _) => const ChatPage(),
          ),
          GoRoute(
            path: '/sessions/:sid',
            builder: (_, s) => ChatPage(
              sessionId: s.pathParameters['sid'],
              // ?msg=<id>：收藏/笔记"跳回原消息"时滚到该消息并高亮。
              highlightMessageId: s.uri.queryParameters['msg'],
            ),
          ),
          GoRoute(
            path: '/admin',
            builder: (_, _) => const AdminDashboard(),
          ),
        ],
      ),
      // 收藏 / 笔记与 AppShell 平级：自带 AppBar（含 back），push 进入、返回回到来源页。
      GoRoute(
        path: '/favorites',
        builder: (_, _) => const FavoritesPage(),
      ),
      GoRoute(
        path: '/notes',
        builder: (_, _) => const NotesPage(),
      ),
      // Reader 与 AppShell 平级：自带 AppBar + 左侧 TocDrawer，避免双 Drawer 嵌套。
      // 顶部 AppBar back 按钮回 /chat。
      GoRoute(
        path: '/reader/:spec',
        builder: (_, s) => ReaderPage(
          specId: s.pathParameters['spec']!,
          activeChunkId: _parseChunkAnchor(s.uri.fragment),
        ),
      ),
      GoRoute(
        path: '/reader/:spec/:section',
        builder: (_, s) => ReaderPage(
          specId: s.pathParameters['spec']!,
          sectionPath: s.pathParameters['section'],
          activeChunkId: _parseChunkAnchor(s.uri.fragment),
        ),
      ),
    ],
  );
});

/// URL fragment `chunk-xxx` → `xxx`；不匹配 → null。
///
/// 用 fragment 而非 query 是因为 go_router state 的 `uri.fragment` 在路径切换时
/// 即使 path 相同也会让 ReaderPage 重建（key 中绑 fragment），触发滚到锚点 +
/// 高亮淡出。
String? _parseChunkAnchor(String fragment) {
  if (fragment.isEmpty) return null;
  if (fragment.startsWith('chunk-')) return fragment.substring(6);
  return null;
}
