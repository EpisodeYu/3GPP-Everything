import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';

/// 单测注入：真 AuthController.build 会读 flutter_secure_storage（platform channel
/// 在 flutter_test 里没初始化）→ 抛 "Binding has not yet been initialized"。
/// 凡是订阅 chatControllerProvider / sessionsControllerProvider / 任何 watch
/// authControllerProvider.future 的 widget/controller 的测试，都必须 override 它。
///
/// 用法（ProviderContainer）：
/// ```dart
/// ProviderContainer(overrides: [
///   authControllerProvider.overrideWith(FakeAuthController.new),
///   ...,
/// ]);
/// ```
///
/// 用法（ProviderScope widget test）：
/// ```dart
/// ProviderScope(
///   overrides: [
///     authControllerProvider.overrideWith(FakeAuthController.new),
///     ...,
///   ],
///   child: ...,
/// );
/// ```
class FakeAuthController extends AuthController {
  @override
  Future<AuthState> build() async => AuthAuthenticated(
        Me(
          id: 'u-test',
          username: 'tester',
          role: 'user',
          isActive: true,
          createdAt: DateTime.utc(2026, 1, 1),
        ),
      );
}

/// 锚定的 override，避免每个测试文件都得重复写 `.overrideWith(FakeAuthController.new)`。
final fakeAuthControllerOverride =
    authControllerProvider.overrideWith(FakeAuthController.new);
