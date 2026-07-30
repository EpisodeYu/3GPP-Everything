import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/l10n/app_localizations.dart';
import '../../core/theme.dart';
import '../../data/api/auth_api.dart';
import '../../domain/auth/auth_controller.dart';
import '../../domain/auth/auth_state.dart';

const _pageBackground = Color(0xFF000000);
const _panelBackground = Color(0xFF0D0F12);
const _fieldBackground = Color(0xFF171A1F);
const _borderColor = Color(0xFF292D35);
const _mutedText = Color(0xFFA5ABB5);
const _brandBlue = Color(0xFF3B82F6);
const _brandTeal = Color(0xFF2DD4BF);

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
  final _passwordFocus = FocusNode();
  bool _bootstrapOpen = false;
  bool _passwordVisible = false;

  @override
  void dispose() {
    _username.dispose();
    _password.dispose();
    _inviteCode.dispose();
    _passwordFocus.dispose();
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

    // 登录页是品牌入口，固定使用独立暗色视觉，不跟随应用内的主题偏好。
    return Theme(
      data: _loginTheme(),
      child: AnnotatedRegion<SystemUiOverlayStyle>(
        value: SystemUiOverlayStyle.light.copyWith(
          statusBarColor: _pageBackground,
          systemNavigationBarColor: _pageBackground,
          systemNavigationBarIconBrightness: Brightness.light,
        ),
        child: Scaffold(
          backgroundColor: _pageBackground,
          body: SafeArea(
            child: LayoutBuilder(
              builder: (context, constraints) {
                final compact = constraints.maxWidth < 600;
                final keyboardOpen =
                    MediaQuery.viewInsetsOf(context).bottom > 0;
                final topPadding = keyboardOpen
                    ? 24.0
                    : kIsWeb
                    ? (constraints.maxHeight * 0.1).clamp(56.0, 104.0)
                    : (constraints.maxHeight * 0.08).clamp(36.0, 72.0);

                return SingleChildScrollView(
                  keyboardDismissBehavior:
                      ScrollViewKeyboardDismissBehavior.onDrag,
                  padding: EdgeInsets.fromLTRB(16, topPadding, 16, 32),
                  child: Align(
                    alignment: Alignment.topCenter,
                    child: SizedBox(
                      width: 400,
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Center(child: _BrandMark(compact: compact)),
                          SizedBox(height: compact ? 18 : 22),
                          Text(
                            '3GPP-Everything',
                            key: const Key('login_brand_title'),
                            textAlign: TextAlign.center,
                            style: Theme.of(context).textTheme.headlineMedium
                                ?.copyWith(
                                  color: Colors.white,
                                  fontWeight: FontWeight.w700,
                                  height: 1.1,
                                  letterSpacing: -0.8,
                                ),
                          ),
                          const SizedBox(height: 10),
                          Text(
                            'Production-grade RAG over 3GPP specs',
                            key: const Key('login_brand_tagline'),
                            textAlign: TextAlign.center,
                            style: Theme.of(context).textTheme.bodyMedium
                                ?.copyWith(
                                  color: _mutedText,
                                  letterSpacing: 0.1,
                                ),
                          ),
                          SizedBox(height: compact ? 30 : 36),
                          DecoratedBox(
                            key: const Key('login_panel'),
                            decoration: BoxDecoration(
                              color: _panelBackground,
                              borderRadius: BorderRadius.circular(18),
                              border: Border.all(color: _borderColor),
                              boxShadow: const [
                                BoxShadow(
                                  color: Color(0x66000000),
                                  blurRadius: 30,
                                  offset: Offset(0, 14),
                                ),
                              ],
                            ),
                            child: Padding(
                              padding: EdgeInsets.all(compact ? 22 : 28),
                              child: AutofillGroup(
                                child: Form(
                                  key: _formKey,
                                  child: Column(
                                    mainAxisSize: MainAxisSize.min,
                                    crossAxisAlignment:
                                        CrossAxisAlignment.stretch,
                                    children: [
                                      TextFormField(
                                        key: const Key('login_username'),
                                        controller: _username,
                                        enabled: !loading,
                                        autofillHints: const [
                                          AutofillHints.username,
                                        ],
                                        autocorrect: false,
                                        textInputAction: TextInputAction.next,
                                        decoration: InputDecoration(
                                          labelText: t.loginUsernameLabel,
                                          prefixIcon: const Icon(
                                            Icons.person_outline_rounded,
                                          ),
                                        ),
                                        validator: (v) =>
                                            (v == null || v.trim().isEmpty)
                                            ? t.loginUsernameRequired
                                            : null,
                                        onFieldSubmitted: (_) =>
                                            _passwordFocus.requestFocus(),
                                      ),
                                      const SizedBox(height: 16),
                                      TextFormField(
                                        key: const Key('login_password'),
                                        controller: _password,
                                        focusNode: _passwordFocus,
                                        enabled: !loading,
                                        obscureText: !_passwordVisible,
                                        autofillHints: const [
                                          AutofillHints.password,
                                        ],
                                        autocorrect: false,
                                        enableSuggestions: false,
                                        textInputAction: TextInputAction.done,
                                        decoration: InputDecoration(
                                          labelText: t.loginPasswordLabel,
                                          prefixIcon: const Icon(
                                            Icons.lock_outline_rounded,
                                          ),
                                          suffixIcon: IconButton(
                                            key: const Key(
                                              'login_password_visibility',
                                            ),
                                            tooltip: _passwordVisible
                                                ? t.loginHidePassword
                                                : t.loginShowPassword,
                                            onPressed: loading
                                                ? null
                                                : () => setState(
                                                    () => _passwordVisible =
                                                        !_passwordVisible,
                                                  ),
                                            icon: Icon(
                                              _passwordVisible
                                                  ? Icons
                                                        .visibility_off_outlined
                                                  : Icons.visibility_outlined,
                                            ),
                                          ),
                                        ),
                                        validator: (v) =>
                                            (v == null || v.isEmpty)
                                            ? t.loginPasswordRequired
                                            : null,
                                        onFieldSubmitted: (_) {
                                          if (!loading) _onLogin();
                                        },
                                      ),
                                      if (errorMessage != null) ...[
                                        const SizedBox(height: 14),
                                        Semantics(
                                          liveRegion: true,
                                          child: Row(
                                            key: const Key('login_error'),
                                            crossAxisAlignment:
                                                CrossAxisAlignment.start,
                                            children: [
                                              Icon(
                                                Icons.error_outline_rounded,
                                                size: 18,
                                                color: Theme.of(
                                                  context,
                                                ).colorScheme.error,
                                              ),
                                              const SizedBox(width: 8),
                                              Expanded(
                                                child: Text(
                                                  errorMessage,
                                                  style: TextStyle(
                                                    color: Theme.of(
                                                      context,
                                                    ).colorScheme.error,
                                                  ),
                                                ),
                                              ),
                                            ],
                                          ),
                                        ),
                                      ],
                                      const SizedBox(height: 24),
                                      _GradientLoginButton(
                                        key: const Key('login_submit'),
                                        onPressed: loading ? null : _onLogin,
                                        loading: loading,
                                        label: t.loginSubmit,
                                      ),
                                      // 仅在 users 表为空时显示首次初始化入口。
                                      ref
                                          .watch(bootstrapStatusProvider)
                                          .maybeWhen(
                                            data: (needsBootstrap) =>
                                                needsBootstrap
                                                ? Padding(
                                                    padding:
                                                        const EdgeInsets.only(
                                                          top: 20,
                                                        ),
                                                    child: _BootstrapPanel(
                                                      isOpen: _bootstrapOpen,
                                                      onToggle: () => setState(
                                                        () => _bootstrapOpen =
                                                            !_bootstrapOpen,
                                                      ),
                                                      inviteCodeController:
                                                          _inviteCode,
                                                      loading: loading,
                                                      onSubmit: _onBootstrap,
                                                    ),
                                                  )
                                                : const SizedBox.shrink(),
                                            orElse: () =>
                                                const SizedBox.shrink(),
                                          ),
                                    ],
                                  ),
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
        ),
      ),
    );
  }

  void _onLogin() {
    if (!_formKey.currentState!.validate()) return;
    ref
        .read(authControllerProvider.notifier)
        .login(username: _username.text.trim(), password: _password.text);
  }

  void _onBootstrap() {
    if (!_formKey.currentState!.validate()) return;
    if (_inviteCode.text.trim().isEmpty) return;
    ref
        .read(authControllerProvider.notifier)
        .bootstrapAdmin(
          username: _username.text.trim(),
          password: _password.text,
          inviteCode: _inviteCode.text.trim(),
        );
  }
}

ThemeData _loginTheme() {
  final base = AppTheme.dark();
  const scheme = ColorScheme.dark(
    primary: _brandBlue,
    secondary: _brandTeal,
    surface: _pageBackground,
    onSurface: Color(0xFFF4F6F8),
    onSurfaceVariant: _mutedText,
    error: Color(0xFFFF7B83),
    outline: _borderColor,
    outlineVariant: Color(0xFF22262D),
  );
  final fieldBorder = OutlineInputBorder(
    borderRadius: BorderRadius.circular(12),
    borderSide: const BorderSide(color: _borderColor),
  );

  return base.copyWith(
    colorScheme: scheme,
    scaffoldBackgroundColor: _pageBackground,
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: _fieldBackground,
      labelStyle: const TextStyle(color: _mutedText),
      floatingLabelStyle: const TextStyle(color: _brandBlue),
      prefixIconColor: _mutedText,
      suffixIconColor: _mutedText,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 17),
      border: fieldBorder,
      enabledBorder: fieldBorder,
      disabledBorder: fieldBorder.copyWith(
        borderSide: const BorderSide(color: Color(0xFF20242A)),
      ),
      focusedBorder: fieldBorder.copyWith(
        borderSide: const BorderSide(color: _brandBlue, width: 1.6),
      ),
      errorBorder: fieldBorder.copyWith(
        borderSide: const BorderSide(color: Color(0xFFFF7B83)),
      ),
      focusedErrorBorder: fieldBorder.copyWith(
        borderSide: const BorderSide(color: Color(0xFFFF7B83), width: 1.6),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: const Color(0xFFD7DAE0),
        side: const BorderSide(color: Color(0xFF343942)),
        minimumSize: const Size.fromHeight(46),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: _mutedText,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    ),
    textSelectionTheme: const TextSelectionThemeData(
      cursorColor: _brandBlue,
      selectionColor: Color(0x663B82F6),
      selectionHandleColor: _brandBlue,
    ),
  );
}

/// 使用 PWA 的透明品牌标，裁掉源文件四周为应用图标预留的透明安全区。
class _BrandMark extends StatelessWidget {
  const _BrandMark({required this.compact});

  final bool compact;

  @override
  Widget build(BuildContext context) {
    final width = compact ? 96.0 : 112.0;
    return ClipRect(
      child: SizedBox(
        width: width,
        height: width * 0.76,
        child: OverflowBox(
          maxWidth: width * 1.67,
          maxHeight: width * 1.67,
          child: Image.asset(
            'web/icons/Icon-512.png',
            key: const Key('login_brand_mark'),
            width: width * 1.67,
            height: width * 1.67,
            filterQuality: FilterQuality.high,
            semanticLabel: '3GPP-Everything',
          ),
        ),
      ),
    );
  }
}

class _GradientLoginButton extends StatelessWidget {
  const _GradientLoginButton({
    super.key,
    required this.onPressed,
    required this.loading,
    required this.label,
  });

  final VoidCallback? onPressed;
  final bool loading;
  final String label;

  @override
  Widget build(BuildContext context) {
    final enabled = onPressed != null;
    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: enabled
            ? const LinearGradient(colors: [_brandBlue, _brandTeal])
            : const LinearGradient(
                colors: [Color(0xFF283242), Color(0xFF243B3A)],
              ),
        borderRadius: BorderRadius.circular(12),
        boxShadow: enabled
            ? const [
                BoxShadow(
                  color: Color(0x333B82F6),
                  blurRadius: 18,
                  offset: Offset(0, 7),
                ),
              ]
            : null,
      ),
      child: FilledButton(
        onPressed: onPressed,
        style: ButtonStyle(
          minimumSize: const WidgetStatePropertyAll(Size.fromHeight(52)),
          elevation: const WidgetStatePropertyAll(0),
          backgroundColor: const WidgetStatePropertyAll(Colors.transparent),
          foregroundColor: WidgetStatePropertyAll(
            enabled ? Colors.white : const Color(0xFF8B919B),
          ),
          overlayColor: WidgetStateProperty.resolveWith(
            (states) => states.contains(WidgetState.pressed)
                ? Colors.white.withValues(alpha: 0.12)
                : null,
          ),
          shape: WidgetStatePropertyAll(
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          ),
        ),
        child: loading
            ? const SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(
                  color: Colors.white,
                  strokeWidth: 2,
                ),
              )
            : Text(label),
      ),
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
    return Container(
      decoration: BoxDecoration(
        color: const Color(0xFF111419),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFF252A31)),
      ),
      child: Column(
        children: [
          Material(
            type: MaterialType.transparency,
            child: ListTile(
              key: const Key('bootstrap_toggle'),
              dense: true,
              contentPadding: const EdgeInsets.symmetric(horizontal: 14),
              title: Text(t.bootstrapToggle),
              titleTextStyle: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: _mutedText,
                fontWeight: FontWeight.w500,
              ),
              trailing: Icon(
                isOpen ? Icons.expand_less_rounded : Icons.expand_more_rounded,
                size: 20,
              ),
              onTap: onToggle,
            ),
          ),
          if (isOpen)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 2, 14, 14),
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
