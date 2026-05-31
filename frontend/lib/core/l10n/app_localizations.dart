import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/intl.dart' as intl;

import 'app_localizations_en.dart';
import 'app_localizations_zh.dart';

// ignore_for_file: type=lint

/// Callers can lookup localized strings with an instance of AppLocalizations
/// returned by `AppLocalizations.of(context)`.
///
/// Applications need to include `AppLocalizations.delegate()` in their app's
/// `localizationDelegates` list, and the locales they support in the app's
/// `supportedLocales` list. For example:
///
/// ```dart
/// import 'l10n/app_localizations.dart';
///
/// return MaterialApp(
///   localizationsDelegates: AppLocalizations.localizationsDelegates,
///   supportedLocales: AppLocalizations.supportedLocales,
///   home: MyApplicationHome(),
/// );
/// ```
///
/// ## Update pubspec.yaml
///
/// Please make sure to update your pubspec.yaml to include the following
/// packages:
///
/// ```yaml
/// dependencies:
///   # Internationalization support.
///   flutter_localizations:
///     sdk: flutter
///   intl: any # Use the pinned version from flutter_localizations
///
///   # Rest of dependencies
/// ```
///
/// ## iOS Applications
///
/// iOS applications define key application metadata, including supported
/// locales, in an Info.plist file that is built into the application bundle.
/// To configure the locales supported by your app, you’ll need to edit this
/// file.
///
/// First, open your project’s ios/Runner.xcworkspace Xcode workspace file.
/// Then, in the Project Navigator, open the Info.plist file under the Runner
/// project’s Runner folder.
///
/// Next, select the Information Property List item, select Add Item from the
/// Editor menu, then select Localizations from the pop-up menu.
///
/// Select and expand the newly-created Localizations item then, for each
/// locale your application supports, add a new item and select the locale
/// you wish to add from the pop-up menu in the Value field. This list should
/// be consistent with the languages listed in the AppLocalizations.supportedLocales
/// property.
abstract class AppLocalizations {
  AppLocalizations(String locale)
    : localeName = intl.Intl.canonicalizedLocale(locale.toString());

  final String localeName;

  static AppLocalizations of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations)!;
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// A list of this localizations delegate along with the default localizations
  /// delegates.
  ///
  /// Returns a list of localizations delegates containing this delegate along with
  /// GlobalMaterialLocalizations.delegate, GlobalCupertinoLocalizations.delegate,
  /// and GlobalWidgetsLocalizations.delegate.
  ///
  /// Additional delegates can be added by appending to this list in
  /// MaterialApp. This list does not have to be used at all if a custom list
  /// of delegates is preferred or required.
  static const List<LocalizationsDelegate<dynamic>> localizationsDelegates =
      <LocalizationsDelegate<dynamic>>[
        delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
      ];

  /// A list of this localizations delegate's supported locales.
  static const List<Locale> supportedLocales = <Locale>[
    Locale('en'),
    Locale('zh'),
  ];

  /// No description provided for @appTitle.
  ///
  /// In en, this message translates to:
  /// **'3GPP Everything'**
  String get appTitle;

  /// No description provided for @loginSubtitle.
  ///
  /// In en, this message translates to:
  /// **'Sign in to continue'**
  String get loginSubtitle;

  /// No description provided for @loginUsernameLabel.
  ///
  /// In en, this message translates to:
  /// **'Username'**
  String get loginUsernameLabel;

  /// No description provided for @loginPasswordLabel.
  ///
  /// In en, this message translates to:
  /// **'Password'**
  String get loginPasswordLabel;

  /// No description provided for @loginUsernameRequired.
  ///
  /// In en, this message translates to:
  /// **'Please enter a username'**
  String get loginUsernameRequired;

  /// No description provided for @loginPasswordRequired.
  ///
  /// In en, this message translates to:
  /// **'Please enter a password'**
  String get loginPasswordRequired;

  /// No description provided for @loginSubmit.
  ///
  /// In en, this message translates to:
  /// **'Sign in'**
  String get loginSubmit;

  /// No description provided for @bootstrapToggle.
  ///
  /// In en, this message translates to:
  /// **'First deployment? Create an admin'**
  String get bootstrapToggle;

  /// No description provided for @bootstrapInviteLabel.
  ///
  /// In en, this message translates to:
  /// **'Invite code (BOOTSTRAP_ADMIN_INVITE_CODE)'**
  String get bootstrapInviteLabel;

  /// No description provided for @bootstrapSubmit.
  ///
  /// In en, this message translates to:
  /// **'Create admin & sign in'**
  String get bootstrapSubmit;

  /// No description provided for @sidebarNewSession.
  ///
  /// In en, this message translates to:
  /// **'New session'**
  String get sidebarNewSession;

  /// No description provided for @sidebarOpenReader.
  ///
  /// In en, this message translates to:
  /// **'Reader'**
  String get sidebarOpenReader;

  /// No description provided for @sidebarOpenAdmin.
  ///
  /// In en, this message translates to:
  /// **'Admin'**
  String get sidebarOpenAdmin;

  /// No description provided for @sidebarLogout.
  ///
  /// In en, this message translates to:
  /// **'Sign out'**
  String get sidebarLogout;

  /// No description provided for @sidebarSessionsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No sessions yet. Tap \"New session\" above to start.'**
  String get sidebarSessionsEmpty;

  /// No description provided for @sidebarSessionsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Failed to load sessions'**
  String get sidebarSessionsLoadError;

  /// No description provided for @sidebarRetry.
  ///
  /// In en, this message translates to:
  /// **'Retry'**
  String get sidebarRetry;

  /// No description provided for @sidebarArchivedGroup.
  ///
  /// In en, this message translates to:
  /// **'Forked history'**
  String get sidebarArchivedGroup;

  /// No description provided for @sidebarSessionMenuRename.
  ///
  /// In en, this message translates to:
  /// **'Rename'**
  String get sidebarSessionMenuRename;

  /// No description provided for @sidebarSessionMenuDelete.
  ///
  /// In en, this message translates to:
  /// **'Delete'**
  String get sidebarSessionMenuDelete;

  /// No description provided for @sidebarRoleLabel.
  ///
  /// In en, this message translates to:
  /// **'role={role}'**
  String sidebarRoleLabel(String role);

  /// No description provided for @renameDialogTitle.
  ///
  /// In en, this message translates to:
  /// **'Rename session'**
  String get renameDialogTitle;

  /// No description provided for @renameDialogLabel.
  ///
  /// In en, this message translates to:
  /// **'New title'**
  String get renameDialogLabel;

  /// No description provided for @renameDialogCancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get renameDialogCancel;

  /// No description provided for @renameDialogSave.
  ///
  /// In en, this message translates to:
  /// **'Save'**
  String get renameDialogSave;

  /// No description provided for @deleteDialogTitle.
  ///
  /// In en, this message translates to:
  /// **'Delete session'**
  String get deleteDialogTitle;

  /// No description provided for @deleteDialogContent.
  ///
  /// In en, this message translates to:
  /// **'Delete \"{title}\"? This action cannot be undone.'**
  String deleteDialogContent(String title);

  /// No description provided for @deleteDialogCancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get deleteDialogCancel;

  /// No description provided for @deleteDialogConfirm.
  ///
  /// In en, this message translates to:
  /// **'Delete'**
  String get deleteDialogConfirm;

  /// No description provided for @sidebarDeleteAll.
  ///
  /// In en, this message translates to:
  /// **'Clear all sessions'**
  String get sidebarDeleteAll;

  /// No description provided for @deleteAllDialogTitle.
  ///
  /// In en, this message translates to:
  /// **'Clear all sessions'**
  String get deleteAllDialogTitle;

  /// No description provided for @deleteAllDialogContent.
  ///
  /// In en, this message translates to:
  /// **'Permanently delete all {count} sessions and their messages for this account. This action cannot be undone.'**
  String deleteAllDialogContent(int count);

  /// No description provided for @deleteAllDialogConfirm.
  ///
  /// In en, this message translates to:
  /// **'Clear'**
  String get deleteAllDialogConfirm;

  /// No description provided for @snackbarDeleteAllSuccess.
  ///
  /// In en, this message translates to:
  /// **'Cleared {count} sessions'**
  String snackbarDeleteAllSuccess(int count);

  /// No description provided for @snackbarDeleteAllFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to clear: {error}'**
  String snackbarDeleteAllFailed(String error);

  /// No description provided for @chatEmptyTitle.
  ///
  /// In en, this message translates to:
  /// **'Start a new conversation'**
  String get chatEmptyTitle;

  /// No description provided for @messageStatusCancelled.
  ///
  /// In en, this message translates to:
  /// **'Cancelled'**
  String get messageStatusCancelled;

  /// No description provided for @messageStatusFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed'**
  String get messageStatusFailed;

  /// No description provided for @reasoningClassify.
  ///
  /// In en, this message translates to:
  /// **'Classifying query'**
  String get reasoningClassify;

  /// No description provided for @reasoningRewrite.
  ///
  /// In en, this message translates to:
  /// **'Rewriting question'**
  String get reasoningRewrite;

  /// No description provided for @reasoningHyde.
  ///
  /// In en, this message translates to:
  /// **'Drafting hypothetical answer'**
  String get reasoningHyde;

  /// No description provided for @reasoningMultiQuery.
  ///
  /// In en, this message translates to:
  /// **'Splitting sub-queries'**
  String get reasoningMultiQuery;

  /// No description provided for @reasoningRetrieve.
  ///
  /// In en, this message translates to:
  /// **'Searching 3GPP corpus'**
  String get reasoningRetrieve;

  /// No description provided for @reasoningRerank.
  ///
  /// In en, this message translates to:
  /// **'Reranking candidates'**
  String get reasoningRerank;

  /// No description provided for @reasoningGenerate.
  ///
  /// In en, this message translates to:
  /// **'Drafting answer'**
  String get reasoningGenerate;

  /// No description provided for @reasoningSelfRag.
  ///
  /// In en, this message translates to:
  /// **'Self-checking answer'**
  String get reasoningSelfRag;

  /// No description provided for @reasoningToolDispatch.
  ///
  /// In en, this message translates to:
  /// **'Calling tools'**
  String get reasoningToolDispatch;

  /// No description provided for @reasoningCollapsedTitle.
  ///
  /// In en, this message translates to:
  /// **'Thought for {seconds}s · {steps} steps'**
  String reasoningCollapsedTitle(String seconds, int steps);

  /// No description provided for @reasoningExpand.
  ///
  /// In en, this message translates to:
  /// **'Expand'**
  String get reasoningExpand;

  /// No description provided for @reasoningCollapse.
  ///
  /// In en, this message translates to:
  /// **'Collapse'**
  String get reasoningCollapse;

  /// No description provided for @reasoningWaiting.
  ///
  /// In en, this message translates to:
  /// **'Starting...'**
  String get reasoningWaiting;

  /// No description provided for @reasoningClassifyDone.
  ///
  /// In en, this message translates to:
  /// **'Class: {queryClass} ({complexity}) · rewrite: {query}'**
  String reasoningClassifyDone(
    String queryClass,
    String complexity,
    String query,
  );

  /// No description provided for @reasoningRewriteDone.
  ///
  /// In en, this message translates to:
  /// **'Rewritten: {query}'**
  String reasoningRewriteDone(String query);

  /// No description provided for @reasoningMultiQueryDone.
  ///
  /// In en, this message translates to:
  /// **'Split into {count} sub-queries'**
  String reasoningMultiQueryDone(int count);

  /// No description provided for @reasoningRetrieveDone.
  ///
  /// In en, this message translates to:
  /// **'Found {count} candidates'**
  String reasoningRetrieveDone(int count);

  /// No description provided for @reasoningRerankDone.
  ///
  /// In en, this message translates to:
  /// **'Top-{count} reranked'**
  String reasoningRerankDone(int count);

  /// No description provided for @reasoningSelfRagDone.
  ///
  /// In en, this message translates to:
  /// **'Verdict: {verdict} · confidence {confidence}'**
  String reasoningSelfRagDone(String verdict, String confidence);

  /// No description provided for @themeLight.
  ///
  /// In en, this message translates to:
  /// **'Light'**
  String get themeLight;

  /// No description provided for @themeDark.
  ///
  /// In en, this message translates to:
  /// **'Dark'**
  String get themeDark;

  /// No description provided for @themeSystem.
  ///
  /// In en, this message translates to:
  /// **'Follow system'**
  String get themeSystem;

  /// No description provided for @themeTooltip.
  ///
  /// In en, this message translates to:
  /// **'Theme'**
  String get themeTooltip;

  /// No description provided for @languageEnglish.
  ///
  /// In en, this message translates to:
  /// **'English'**
  String get languageEnglish;

  /// No description provided for @languageChinese.
  ///
  /// In en, this message translates to:
  /// **'中文'**
  String get languageChinese;

  /// No description provided for @languageTooltip.
  ///
  /// In en, this message translates to:
  /// **'Language'**
  String get languageTooltip;

  /// No description provided for @snackbarCreateSessionFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to create session: {error}'**
  String snackbarCreateSessionFailed(String error);

  /// No description provided for @snackbarRenameFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to rename: {error}'**
  String snackbarRenameFailed(String error);

  /// No description provided for @snackbarDeleteFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to delete: {error}'**
  String snackbarDeleteFailed(String error);
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  Future<AppLocalizations> load(Locale locale) {
    return SynchronousFuture<AppLocalizations>(lookupAppLocalizations(locale));
  }

  @override
  bool isSupported(Locale locale) =>
      <String>['en', 'zh'].contains(locale.languageCode);

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}

AppLocalizations lookupAppLocalizations(Locale locale) {
  // Lookup logic when only language code is specified.
  switch (locale.languageCode) {
    case 'en':
      return AppLocalizationsEn();
    case 'zh':
      return AppLocalizationsZh();
  }

  throw FlutterError(
    'AppLocalizations.delegate failed to load unsupported locale "$locale". This is likely '
    'an issue with the localizations generation tool. Please file an issue '
    'on GitHub with a reproducible sample app and the gen-l10n configuration '
    'that was used.',
  );
}
