import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/admin_api.dart';
import 'package:tgpp/data/api/auth_api.dart';
import 'package:tgpp/data/api/docs_api.dart';
import 'package:tgpp/domain/auth/auth_controller.dart';
import 'package:tgpp/domain/auth/auth_state.dart';
import 'package:tgpp/features/admin/admin_dashboard.dart';

import '../../support/fake_admin_api.dart';
import '../../support/fake_docs_api.dart';
import '../../support/localized.dart';

/// Admin 4 路由集成测：dashboard 渲染 4 个 tab；每个 tab 切到时调对应 API。
///
/// 锚：`docs/03-development/05-frontend.md §0 M5.5 完成度门禁`。

class _StubAuthControllerAdmin extends AuthController {
  @override
  Future<AuthState> build() async => AuthAuthenticated(
        Me(
          id: '00000000-0000-0000-0000-000000000001',
          username: 'alice',
          role: 'admin',
          isActive: true,
          createdAt: DateTime.utc(2026, 5, 24),
        ),
      );
}

class _StubAuthControllerUser extends AuthController {
  @override
  Future<AuthState> build() async => AuthAuthenticated(
        Me(
          id: '00000000-0000-0000-0000-000000000002',
          username: 'bob',
          role: 'user',
          isActive: true,
          createdAt: DateTime.utc(2026, 5, 24),
        ),
      );
}

Future<void> _pumpDashboard(
  WidgetTester tester, {
  FakeAdminApi? adminApi,
  FakeDocsApi? docsApi,
  AuthController Function() authCtor = _StubAuthControllerAdmin.new,
  Size size = const Size(1280, 900),
}) async {
  await tester.binding.setSurfaceSize(size);
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        adminApiProvider.overrideWithValue(adminApi ?? FakeAdminApi()),
        docsApiProvider.overrideWithValue(docsApi ?? FakeDocsApi()),
        authControllerProvider.overrideWith(authCtor),
      ],
      child: localizedMaterialApp(home: const AdminDashboard()),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  group('AdminDashboard', () {
    testWidgets('admin 看到 5 个 Tab', (tester) async {
      await _pumpDashboard(tester);

      expect(find.byKey(const Key('admin_tab_bar')), findsOneWidget);
      expect(find.byKey(const Key('admin_tab_docs')), findsOneWidget);
      expect(find.byKey(const Key('admin_tab_tasks')), findsOneWidget);
      expect(find.byKey(const Key('admin_tab_usage')), findsOneWidget);
      expect(find.byKey(const Key('admin_tab_feedback')), findsOneWidget);
      expect(find.byKey(const Key('admin_tab_tools')), findsOneWidget);
    });

    testWidgets('反馈 Tab：渲染计数 + 列表 + thumb 过滤', (tester) async {
      final admin = FakeAdminApi(
        feedback: buildFeedback(up: 3, down: 1, items: [
          buildFeedbackItem(id: 'fb-1', thumb: 1, messagePreview: '好答案'),
          buildFeedbackItem(
            id: 'fb-2',
            thumb: -1,
            reason: '答非所问',
            messagePreview: '坏答案',
          ),
        ]),
      );
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_feedback')));
      await tester.pumpAndSettle();

      expect(admin.getFeedbackCalls, greaterThanOrEqualTo(1));
      expect(find.byKey(const Key('admin_feedback_up')), findsOneWidget);
      expect(find.byKey(const Key('admin_feedback_down')), findsOneWidget);
      expect(find.byKey(const Key('admin_feedback_item_fb-1')), findsOneWidget);
      expect(find.byKey(const Key('admin_feedback_item_fb-2')), findsOneWidget);
      expect(find.textContaining('答非所问'), findsOneWidget);

      // 点"点踩"过滤 → 以 thumb=-1 重新拉取
      await tester.tap(find.byKey(const Key('admin_feedback_filter_down')));
      await tester.pumpAndSettle();
      expect(admin.lastFeedbackThumb, -1);
    });

    testWidgets('反馈 Tab：API 错误 → 显示重试', (tester) async {
      final admin = FakeAdminApi()..feedbackErr = StateError('fb down');
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_feedback')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_feedback_error')), findsOneWidget);
      expect(find.byKey(const Key('admin_feedback_retry')), findsOneWidget);
    });

    testWidgets('非 admin 用户兜底显示 admin_forbidden', (tester) async {
      await _pumpDashboard(tester, authCtor: _StubAuthControllerUser.new);
      expect(find.byKey(const Key('admin_forbidden')), findsOneWidget);
      expect(find.byKey(const Key('admin_tab_bar')), findsNothing);
    });

    testWidgets('文档 Tab：渲染 release/series 过滤 + 走 DocsApi.list', (tester) async {
      final docs = FakeDocsApi(docs: [
        buildDocOut(specId: '23.501', release: 'Rel-18', series: '23', chunkCount: 1234),
        buildDocOut(specId: '38.331', release: 'Rel-18', series: '38', chunkCount: 999),
      ]);
      await _pumpDashboard(tester, docsApi: docs);

      expect(find.byKey(const Key('admin_docs_release')), findsOneWidget);
      expect(find.byKey(const Key('admin_docs_series')), findsOneWidget);
      expect(find.byKey(const Key('admin_docs_row_23.501')), findsOneWidget);
      expect(find.byKey(const Key('admin_docs_row_38.331')), findsOneWidget);
      expect(docs.listCalls, greaterThanOrEqualTo(1));

      // 按 series 过滤后刷新
      await tester.enterText(find.byKey(const Key('admin_docs_series')), '23');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_docs_row_23.501')), findsOneWidget);
      expect(find.byKey(const Key('admin_docs_row_38.331')), findsNothing);
    });

    testWidgets('任务 Tab：渲染列表 + 状态过滤 + 详情 bottom sheet', (tester) async {
      final admin = FakeAdminApi(tasks: [
        buildTask(id: 't-1', status: 'running', progress: 42, logTail: 'hello\nworld'),
        buildTask(id: 't-2', status: 'done', progress: 100),
      ]);
      await _pumpDashboard(tester, adminApi: admin);

      // 切到任务 Tab
      await tester.tap(find.byKey(const Key('admin_tab_tasks')));
      await tester.pumpAndSettle();

      expect(admin.listTasksCalls, greaterThanOrEqualTo(1));
      expect(find.byKey(const Key('admin_tasks_row_t-1')), findsOneWidget);
      expect(find.byKey(const Key('admin_tasks_row_t-2')), findsOneWidget);

      // 状态过滤
      final callsBefore = admin.listTasksCalls;
      await tester.tap(find.byKey(const Key('admin_tasks_filter_done')));
      await tester.pumpAndSettle();
      expect(admin.listTasksCalls, greaterThan(callsBefore));
      expect(admin.lastTaskFilter, 'done');
      expect(find.byKey(const Key('admin_tasks_row_t-2')), findsOneWidget);
      expect(find.byKey(const Key('admin_tasks_row_t-1')), findsNothing);

      // 点回"全部" → 重置 filter
      await tester.tap(find.byKey(const Key('admin_tasks_filter_all')));
      await tester.pumpAndSettle();
      expect(admin.lastTaskFilter, isNull);

      // 点行 → 详情 sheet
      await tester.tap(find.byKey(const Key('admin_tasks_row_t-1')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('admin_task_detail_id')), findsOneWidget);
      expect(find.byKey(const Key('admin_task_detail_log')), findsOneWidget);
      expect(find.text('hello\nworld'), findsOneWidget);
    });

    testWidgets('统计 Tab：渲染索引/用户/任务/用量卡片', (tester) async {
      final admin = FakeAdminApi(
        stats: buildStats(
          documents: 1270,
          chunks: 394859,
          tasks: const {'queued': 1, 'done': 5},
        ),
      );
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_usage')));
      await tester.pumpAndSettle();

      expect(admin.getStatsCalls, 1);
      expect(find.byKey(const Key('admin_usage_documents')), findsOneWidget);
      expect(find.byKey(const Key('admin_usage_chunks')), findsOneWidget);
      expect(find.byKey(const Key('admin_usage_tasks_done')), findsOneWidget);
      expect(find.byKey(const Key('admin_usage_llm_input')), findsOneWidget);
      expect(find.text('1270'), findsOneWidget);
      expect(find.text('394859'), findsOneWidget);
    });

    testWidgets('工具 Tab：重建索引弹框 → 提交 → 调 triggerIndexRebuild', (tester) async {
      final admin = FakeAdminApi(
        rebuildResult: buildTask(id: 't-new', status: 'queued'),
      );
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_tools')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_rebuild_index_entry')), findsOneWidget);
      expect(find.byKey(const Key('admin_langfuse_link')), findsOneWidget);

      // 打开 rebuild dialog
      await tester.tap(find.byKey(const Key('admin_rebuild_index_entry')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_rebuild_dialog')), findsOneWidget);

      // 填 spec_id + 打开 force
      await tester.enterText(
        find.byKey(const Key('admin_rebuild_spec_input')),
        '23.501',
      );
      await tester.tap(find.byKey(const Key('admin_rebuild_force_switch')));
      await tester.pumpAndSettle();

      // 提交
      await tester.tap(find.byKey(const Key('admin_rebuild_confirm')));
      await tester.pumpAndSettle();

      expect(admin.lastRebuildSpec, '23.501');
      expect(admin.lastRebuildForce, isTrue);
      // 提示 snackbar
      expect(find.textContaining('已提交重建任务'), findsOneWidget);
    });

    testWidgets('重建弹框：取消按钮关闭，不调 API', (tester) async {
      final admin = FakeAdminApi();
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_tools')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('admin_rebuild_index_entry')));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('admin_rebuild_cancel')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_rebuild_dialog')), findsNothing);
      expect(admin.lastRebuildSpec, isNull);
      expect(admin.lastRebuildForce, isNull);
    });

    testWidgets('重建弹框：API 抛错时显示 admin_rebuild_error，不关闭', (tester) async {
      final admin = FakeAdminApi()..rebuildErr = StateError('boom');
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_tools')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('admin_rebuild_index_entry')));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('admin_rebuild_confirm')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_rebuild_error')), findsOneWidget);
      expect(find.byKey(const Key('admin_rebuild_dialog')), findsOneWidget);
    });

    testWidgets('窄屏（安卓）：文档过滤区 Wrap 换行，不 overflow', (tester) async {
      final docs = FakeDocsApi(docs: [
        buildDocOut(
            specId: '23.501', release: 'Rel-18', series: '23', chunkCount: 1234),
      ]);
      await _pumpDashboard(tester, docsApi: docs, size: const Size(360, 800));

      // 无 RenderFlex overflow（溢出会被记成 pending exception）
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('admin_docs_release')), findsOneWidget);
      expect(find.byKey(const Key('admin_docs_series')), findsOneWidget);
    });

    testWidgets('窄屏（安卓）：任务过滤 chips 单行横滑，不 overflow', (tester) async {
      final admin = FakeAdminApi(tasks: [
        buildTask(id: 't-1', status: 'done', progress: 100),
      ]);
      await _pumpDashboard(tester, adminApi: admin, size: const Size(360, 800));

      await tester.tap(find.byKey(const Key('admin_tab_tasks')));
      await tester.pumpAndSettle();

      // 横滑容器消化超宽的 chip 行 → 无 RenderFlex overflow
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('admin_tasks_filter_scroll')), findsOneWidget);
      expect(find.byKey(const Key('admin_tasks_refresh')), findsOneWidget);

      // 右侧 chip 窄屏可能在视口外：横向滑动后可见并可点选
      await tester.scrollUntilVisible(
        find.byKey(const Key('admin_tasks_filter_failed')),
        80,
        scrollable: find.descendant(
          of: find.byKey(const Key('admin_tasks_filter_scroll')),
          matching: find.byType(Scrollable),
        ),
      );
      await tester.tap(find.byKey(const Key('admin_tasks_filter_failed')));
      await tester.pumpAndSettle();
      expect(admin.lastTaskFilter, 'failed');
    });

    testWidgets('统计 Tab：API 错误 → 显示重试按钮', (tester) async {
      final admin = FakeAdminApi()..statsErr = StateError('stats down');
      await _pumpDashboard(tester, adminApi: admin);

      await tester.tap(find.byKey(const Key('admin_tab_usage')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('admin_usage_error')), findsOneWidget);
      expect(find.byKey(const Key('admin_usage_retry')), findsOneWidget);
    });
  });
}
