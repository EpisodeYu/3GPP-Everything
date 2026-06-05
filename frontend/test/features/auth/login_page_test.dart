import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';
import 'package:tgpp/features/auth/login_page.dart';

import '../../support/localized.dart';

class _FakeAuthController extends AuthController {
  String? lastUsername;
  String? lastPassword;
  String? lastInviteCode;
  int loginCalls = 0;
  int bootstrapCalls = 0;
  AuthState finalState = const AuthAnonymous();

  @override
  Future<AuthState> build() async => const AuthAnonymous();

  @override
  Future<void> login({
    required String username,
    required String password,
  }) async {
    loginCalls += 1;
    lastUsername = username;
    lastPassword = password;
    state = const AsyncLoading<AuthState>();
    await Future<void>.delayed(Duration.zero);
    state = AsyncData(finalState);
  }

  @override
  Future<void> bootstrapAdmin({
    required String username,
    required String password,
    required String inviteCode,
  }) async {
    bootstrapCalls += 1;
    lastUsername = username;
    lastPassword = password;
    lastInviteCode = inviteCode;
    state = AsyncData(finalState);
  }
}

Future<_FakeAuthController> _pumpLogin(
  WidgetTester tester, {
  bool needsBootstrap = true,
}) async {
  final fake = _FakeAuthController();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        authControllerProvider.overrideWith(() => fake),
        bootstrapStatusProvider.overrideWith((ref) async => needsBootstrap),
      ],
      child: localizedMaterialApp(home: const LoginPage()),
    ),
  );
  // AsyncNotifier 初始 state 是 AsyncLoading，build() 在 microtask 完成后才切到
  // AsyncData(AuthAnonymous)；bootstrapStatusProvider(FutureProvider) 同样要 microtask
  // 落到 data。pump 两次让两者都 settle（按钮 enabled + 面板按 needsBootstrap 显示）。
  await tester.pump();
  await tester.pump();
  return fake;
}

void main() {
  testWidgets('渲染用户名 / 密码 / 登录按钮，bootstrap 面板默认折叠', (tester) async {
    await _pumpLogin(tester);

    expect(find.byKey(const Key('login_username')), findsOneWidget);
    expect(find.byKey(const Key('login_password')), findsOneWidget);
    expect(find.byKey(const Key('login_submit')), findsOneWidget);
    expect(find.byKey(const Key('bootstrap_toggle')), findsOneWidget);
    expect(find.byKey(const Key('bootstrap_invite')), findsNothing);
  });

  testWidgets('已初始化（needsBootstrap=false）时隐藏创建管理员面板', (tester) async {
    await _pumpLogin(tester, needsBootstrap: false);

    // 登录表单仍在，但 bootstrap 入口被隐藏
    expect(find.byKey(const Key('login_username')), findsOneWidget);
    expect(find.byKey(const Key('bootstrap_toggle')), findsNothing);
  });

  testWidgets('空字段提交不会调用 login', (tester) async {
    final fake = await _pumpLogin(tester);

    await tester.tap(find.byKey(const Key('login_submit')));
    await tester.pump();
    expect(fake.loginCalls, 0);
    expect(find.text('请输入用户名'), findsOneWidget);
    expect(find.text('请输入密码'), findsOneWidget);
  });

  testWidgets('填值后提交调用 login，参数 trim 后传入', (tester) async {
    final fake = await _pumpLogin(tester);

    await tester.enterText(find.byKey(const Key('login_username')), '  alice ');
    await tester.enterText(find.byKey(const Key('login_password')), 'pw123456');
    await tester.tap(find.byKey(const Key('login_submit')));
    await tester.pumpAndSettle();

    expect(fake.loginCalls, 1);
    expect(fake.lastUsername, 'alice');
    expect(fake.lastPassword, 'pw123456');
  });

  testWidgets('AuthAnonymous.errorMessage 渲染为错误提示', (tester) async {
    final fake = await _pumpLogin(tester);
    fake.finalState =
        const AuthAnonymous(errorMessage: 'bad_credentials');

    await tester.enterText(find.byKey(const Key('login_username')), 'alice');
    await tester.enterText(find.byKey(const Key('login_password')), 'pw123456');
    await tester.tap(find.byKey(const Key('login_submit')));
    // 让 login future（含 Future.delayed(Duration.zero)）完成 + UI 重 build
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('login_error')), findsOneWidget);
    expect(find.text('bad_credentials'), findsOneWidget);
  });

  testWidgets('bootstrap 折叠面板展开 + 提交触发 bootstrapAdmin', (tester) async {
    final fake = await _pumpLogin(tester);

    await tester.tap(find.byKey(const Key('bootstrap_toggle')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('bootstrap_invite')), findsOneWidget);

    await tester.enterText(find.byKey(const Key('login_username')), 'admin');
    await tester.enterText(find.byKey(const Key('login_password')), 'pw123456');
    await tester.enterText(
      find.byKey(const Key('bootstrap_invite')),
      'invite-xyz',
    );
    await tester.tap(find.byKey(const Key('bootstrap_submit')));
    await tester.pump();

    expect(fake.bootstrapCalls, 1);
    expect(fake.lastUsername, 'admin');
    expect(fake.lastPassword, 'pw123456');
    expect(fake.lastInviteCode, 'invite-xyz');
  });
}
