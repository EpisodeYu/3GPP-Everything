import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/l10n/app_localizations.dart';
import '../../data/api/auth_api.dart';
import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';

class LoginPage extends ConsumerStatefulWidget {
  const LoginPage({super.key});

  @override
  ConsumerState<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends ConsumerState<LoginPage> {
  final _formKey = GlobalKey<FormState>();
  final _username = TextEditingController();
  final _password = TextEditingController();
  final _inviteCode = TextEditingController();
  bool _bootstrapOpen = false;

  @override
  void dispose() {
    _username.dispose();
    _password.dispose();
    _inviteCode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(authControllerProvider);
    final loading = state.isLoading;
    final errorMessage = state.maybeWhen(
      data: (s) => s is AuthAnonymous ? s.errorMessage : null,
      orElse: () => null,
    );
    final t = AppLocalizations.of(context);

    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Form(
              key: _formKey,
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    t.appTitle,
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    t.loginSubtitle,
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                  const SizedBox(height: 32),
                  TextFormField(
                    key: const Key('login_username'),
                    controller: _username,
                    enabled: !loading,
                    autofillHints: const [AutofillHints.username],
                    decoration: InputDecoration(labelText: t.loginUsernameLabel),
                    validator: (v) => (v == null || v.trim().isEmpty)
                        ? t.loginUsernameRequired
                        : null,
                  ),
                  const SizedBox(height: 16),
                  TextFormField(
                    key: const Key('login_password'),
                    controller: _password,
                    enabled: !loading,
                    obscureText: true,
                    autofillHints: const [AutofillHints.password],
                    decoration: InputDecoration(labelText: t.loginPasswordLabel),
                    validator: (v) => (v == null || v.isEmpty)
                        ? t.loginPasswordRequired
                        : null,
                  ),
                  if (errorMessage != null) ...[
                    const SizedBox(height: 12),
                    Text(
                      errorMessage,
                      key: const Key('login_error'),
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                  ],
                  const SizedBox(height: 24),
                  FilledButton(
                    key: const Key('login_submit'),
                    onPressed: loading ? null : _onLogin,
                    child: loading
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Text(t.loginSubmit),
                  ),
                  // 仅在「未初始化」（users 表为空）的部署显示创建管理员面板；
                  // 已有用户的部署（含本线上站）隐藏该死入口。取不到状态 → 隐藏。
                  ref.watch(bootstrapStatusProvider).maybeWhen(
                        data: (needsBootstrap) => needsBootstrap
                            ? Padding(
                                padding: const EdgeInsets.only(top: 24),
                                child: _BootstrapPanel(
                                  isOpen: _bootstrapOpen,
                                  onToggle: () => setState(
                                      () => _bootstrapOpen = !_bootstrapOpen),
                                  inviteCodeController: _inviteCode,
                                  loading: loading,
                                  onSubmit: _onBootstrap,
                                ),
                              )
                            : const SizedBox.shrink(),
                        orElse: () => const SizedBox.shrink(),
                      ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  void _onLogin() {
    if (!_formKey.currentState!.validate()) return;
    ref.read(authControllerProvider.notifier).login(
          username: _username.text.trim(),
          password: _password.text,
        );
  }

  void _onBootstrap() {
    if (!_formKey.currentState!.validate()) return;
    if (_inviteCode.text.trim().isEmpty) return;
    ref.read(authControllerProvider.notifier).bootstrapAdmin(
          username: _username.text.trim(),
          password: _password.text,
          inviteCode: _inviteCode.text.trim(),
        );
  }
}

class _BootstrapPanel extends StatelessWidget {
  const _BootstrapPanel({
    required this.isOpen,
    required this.onToggle,
    required this.inviteCodeController,
    required this.loading,
    required this.onSubmit,
  });

  final bool isOpen;
  final VoidCallback onToggle;
  final TextEditingController inviteCodeController;
  final bool loading;
  final VoidCallback onSubmit;

  @override
  Widget build(BuildContext context) {
    final t = AppLocalizations.of(context);
    return Card(
      child: Column(
        children: [
          ListTile(
            key: const Key('bootstrap_toggle'),
            title: Text(t.bootstrapToggle),
            trailing: Icon(isOpen ? Icons.expand_less : Icons.expand_more),
            onTap: onToggle,
          ),
          if (isOpen)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  TextField(
                    key: const Key('bootstrap_invite'),
                    controller: inviteCodeController,
                    enabled: !loading,
                    obscureText: true,
                    decoration: InputDecoration(
                      labelText: t.bootstrapInviteLabel,
                    ),
                  ),
                  const SizedBox(height: 12),
                  OutlinedButton(
                    key: const Key('bootstrap_submit'),
                    onPressed: loading ? null : onSubmit,
                    child: Text(t.bootstrapSubmit),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
