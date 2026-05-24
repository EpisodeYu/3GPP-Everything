import '../../data/api/auth_api.dart';

/// 鉴权状态联合体。Dart 3 sealed class，覆盖三种情形：
/// - [AuthAnonymous]：未登录（或 token 失效已清理）
/// - [AuthAuthenticated]：已登录，持有 Me 信息
///
/// 注：加载/请求中态用 `AsyncValue.loading()`/`AsyncValue.error()` 包裹，
/// 因此本联合体只需表达"目前用户身份"。
sealed class AuthState {
  const AuthState();
}

class AuthAnonymous extends AuthState {
  const AuthAnonymous({this.errorMessage});
  final String? errorMessage;
}

class AuthAuthenticated extends AuthState {
  const AuthAuthenticated(this.me);
  final Me me;
}
