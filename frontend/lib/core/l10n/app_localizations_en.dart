// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for English (`en`).
class AppLocalizationsEn extends AppLocalizations {
  AppLocalizationsEn([String locale = 'en']) : super(locale);

  @override
  String get appTitle => '3GPP Everything';

  @override
  String get loginSubtitle => 'Sign in to continue';

  @override
  String get loginUsernameLabel => 'Username';

  @override
  String get loginPasswordLabel => 'Password';

  @override
  String get loginUsernameRequired => 'Please enter a username';

  @override
  String get loginPasswordRequired => 'Please enter a password';

  @override
  String get loginSubmit => 'Sign in';

  @override
  String get bootstrapToggle => 'First deployment? Create an admin';

  @override
  String get bootstrapInviteLabel =>
      'Invite code (BOOTSTRAP_ADMIN_INVITE_CODE)';

  @override
  String get bootstrapSubmit => 'Create admin & sign in';

  @override
  String get sidebarNewSession => 'New session';

  @override
  String get sidebarOpenReader => 'Reader';

  @override
  String get sidebarOpenAdmin => 'Admin';

  @override
  String get sidebarLogout => 'Sign out';

  @override
  String get sidebarSessionsEmpty =>
      'No sessions yet. Tap \"New session\" above to start.';

  @override
  String get sidebarSessionsLoadError => 'Failed to load sessions';

  @override
  String get sidebarRetry => 'Retry';

  @override
  String get sidebarArchivedGroup => 'Forked history';

  @override
  String get sidebarSessionMenuRename => 'Rename';

  @override
  String get sidebarSessionMenuDelete => 'Delete';

  @override
  String sidebarRoleLabel(String role) {
    return 'role=$role';
  }

  @override
  String get renameDialogTitle => 'Rename session';

  @override
  String get renameDialogLabel => 'New title';

  @override
  String get renameDialogCancel => 'Cancel';

  @override
  String get renameDialogSave => 'Save';

  @override
  String get deleteDialogTitle => 'Delete session';

  @override
  String deleteDialogContent(String title) {
    return 'Delete \"$title\"? This action cannot be undone.';
  }

  @override
  String get deleteDialogCancel => 'Cancel';

  @override
  String get deleteDialogConfirm => 'Delete';

  @override
  String get chatEmptyTitle => 'Start a new conversation';

  @override
  String get messageStatusCancelled => 'Cancelled';

  @override
  String get messageStatusFailed => 'Failed';

  @override
  String get themeLight => 'Light';

  @override
  String get themeDark => 'Dark';

  @override
  String get themeSystem => 'Follow system';

  @override
  String get themeTooltip => 'Theme';

  @override
  String get languageEnglish => 'English';

  @override
  String get languageChinese => '中文';

  @override
  String get languageTooltip => 'Language';

  @override
  String snackbarCreateSessionFailed(String error) {
    return 'Failed to create session: $error';
  }

  @override
  String snackbarRenameFailed(String error) {
    return 'Failed to rename: $error';
  }

  @override
  String snackbarDeleteFailed(String error) {
    return 'Failed to delete: $error';
  }
}
