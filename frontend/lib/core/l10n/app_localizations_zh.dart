// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Chinese (`zh`).
class AppLocalizationsZh extends AppLocalizations {
  AppLocalizationsZh([String locale = 'zh']) : super(locale);

  @override
  String get appTitle => '3GPP Everything';

  @override
  String get loginSubtitle => '登录以继续';

  @override
  String get loginUsernameLabel => '用户名';

  @override
  String get loginPasswordLabel => '密码';

  @override
  String get loginUsernameRequired => '请输入用户名';

  @override
  String get loginPasswordRequired => '请输入密码';

  @override
  String get loginSubmit => '登录';

  @override
  String get bootstrapToggle => '首次部署？创建管理员';

  @override
  String get bootstrapInviteLabel => '邀请码 (BOOTSTRAP_ADMIN_INVITE_CODE)';

  @override
  String get bootstrapSubmit => '创建管理员并登录';

  @override
  String get sidebarNewSession => '新会话';

  @override
  String get sidebarOpenReader => '阅读器';

  @override
  String get sidebarOpenAdmin => '管理后台';

  @override
  String get sidebarLogout => '退出登录';

  @override
  String get sidebarSessionsEmpty => '还没有会话，点上方\"新会话\"开始。';

  @override
  String get sidebarSessionsLoadError => '加载会话失败';

  @override
  String get sidebarRetry => '重试';

  @override
  String get sidebarArchivedGroup => '分叉历史';

  @override
  String get sidebarSessionMenuRename => '重命名';

  @override
  String get sidebarSessionMenuDelete => '删除';

  @override
  String sidebarRoleLabel(String role) {
    return 'role=$role';
  }

  @override
  String get renameDialogTitle => '重命名会话';

  @override
  String get renameDialogLabel => '新标题';

  @override
  String get renameDialogCancel => '取消';

  @override
  String get renameDialogSave => '保存';

  @override
  String get deleteDialogTitle => '删除会话';

  @override
  String deleteDialogContent(String title) {
    return '确认删除「$title」？此操作不可撤销。';
  }

  @override
  String get deleteDialogCancel => '取消';

  @override
  String get deleteDialogConfirm => '删除';

  @override
  String get sidebarDeleteAll => '清空全部会话';

  @override
  String get deleteAllDialogTitle => '清空所有会话';

  @override
  String deleteAllDialogContent(int count) {
    return '将永久删除当前账号的全部会话与对话记录（$count 条），此操作不可撤销。';
  }

  @override
  String get deleteAllDialogConfirm => '清空';

  @override
  String snackbarDeleteAllSuccess(int count) {
    return '已清空 $count 个会话';
  }

  @override
  String snackbarDeleteAllFailed(String error) {
    return '清空失败：$error';
  }

  @override
  String get chatEmptyTitle => '开始一个新会话';

  @override
  String get messageStatusCancelled => '已取消';

  @override
  String get messageStatusFailed => '失败';

  @override
  String get reasoningClassify => '判断查询类型';

  @override
  String get reasoningRewrite => '改写问题';

  @override
  String get reasoningHyde => '撰写假设答案';

  @override
  String get reasoningMultiQuery => '拆分子查询';

  @override
  String get reasoningRetrieve => '检索 3GPP 文档';

  @override
  String get reasoningRerank => '精排候选';

  @override
  String get reasoningGenerate => '起草回答';

  @override
  String get reasoningSelfRag => '自校验答案';

  @override
  String get reasoningToolDispatch => '调用工具';

  @override
  String reasoningCollapsedTitle(String seconds, int steps) {
    return '已思考 ${seconds}s · $steps 步骤';
  }

  @override
  String get reasoningExpand => '展开';

  @override
  String get reasoningCollapse => '收起';

  @override
  String get reasoningWaiting => '等待开始...';

  @override
  String reasoningClassifyDone(
    String queryClass,
    String complexity,
    String query,
  ) {
    return '分类:$queryClass ($complexity) · 改写:$query';
  }

  @override
  String reasoningRewriteDone(String query) {
    return '改写为: $query';
  }

  @override
  String reasoningMultiQueryDone(int count) {
    return '拆出 $count 个子查询';
  }

  @override
  String reasoningRetrieveDone(int count) {
    return '找到 $count 个候选';
  }

  @override
  String reasoningRerankDone(int count) {
    return 'Top-$count 排序完成';
  }

  @override
  String reasoningSelfRagDone(String verdict, String confidence) {
    return '自检: $verdict · 置信度 $confidence';
  }

  @override
  String get themeLight => '浅色';

  @override
  String get themeDark => '深色';

  @override
  String get themeSystem => '跟随系统';

  @override
  String get themeTooltip => '主题';

  @override
  String get languageEnglish => 'English';

  @override
  String get languageChinese => '中文';

  @override
  String get languageTooltip => '语言';

  @override
  String snackbarCreateSessionFailed(String error) {
    return '创建会话失败：$error';
  }

  @override
  String snackbarRenameFailed(String error) {
    return '重命名失败：$error';
  }

  @override
  String snackbarDeleteFailed(String error) {
    return '删除失败：$error';
  }
}
