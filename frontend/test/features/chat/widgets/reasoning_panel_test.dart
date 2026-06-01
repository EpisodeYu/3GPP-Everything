import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/core/l10n/app_localizations.dart';
import 'package:tgpp/features/chat/chat_controller.dart';
import 'package:tgpp/features/chat/widgets/reasoning_panel.dart';

Widget _wrap(Widget child, {Locale locale = const Locale('zh')}) {
  return MaterialApp(
    locale: locale,
    localizationsDelegates: AppLocalizations.localizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Scaffold(body: Material(child: child)),
  );
}

void main() {
  testWidgets('空 nodes + 空 reasoning → 不渲染', (tester) async {
    await tester.pumpWidget(_wrap(const ReasoningPanel(
      nodes: [],
      reasoningByNode: {},
      activeNode: null,
      startedAt: null,
      collapsedFromController: false,
    )));
    expect(find.byKey(const Key('reasoning_panel')), findsNothing);
  });

  testWidgets('展开态：渲染节点 chip 列 + 灰色文字区', (tester) async {
    await tester.pumpWidget(_wrap(ReasoningPanel(
      nodes: const [
        NodeRunStatus(
            node: 'classify',
            running: false,
            durationMs: 12,
            summary: {
              'query_class': 'definition',
              'complexity': 'simple',
              'rewritten_query': 'AMF function',
            }),
        NodeRunStatus(node: 'hyde', running: true),
      ],
      reasoningByNode: const {'hyde': 'AMF is the Access...'},
      activeNode: 'hyde',
      startedAt: DateTime.now().subtract(const Duration(seconds: 3)),
      collapsedFromController: false,
    )));
    expect(find.byKey(const Key('reasoning_panel')), findsOneWidget);
    expect(find.byKey(const Key('reasoning_chip_classify')), findsOneWidget);
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);
    // hyde active：灰色文字区显示流式累积内容
    expect(find.textContaining('AMF is the Access...'), findsOneWidget);
    // classify done：summary helper 渲染「分类: definition (simple) · 改写: ...」
    expect(find.textContaining('definition'), findsOneWidget);
    expect(find.byKey(const Key('reasoning_panel_collapse_btn')), findsOneWidget);
  });

  // 注：用 `pump(Duration)` 而不是 `pumpAndSettle()` —— 节点 chip 上的
  // `CircularProgressIndicator`（running 节点的转圈）是无限动画，pumpAndSettle
  // 永远等不到稳态。pump 一帧足够触发 setState / didUpdateWidget。
  testWidgets('折叠态：单行「已思考 X.Xs · N 步骤」+ 上下箭头切换', (tester) async {
    await tester.pumpWidget(_wrap(ReasoningPanel(
      nodes: const [
        NodeRunStatus(node: 'classify', running: false, durationMs: 1),
        NodeRunStatus(node: 'hyde', running: false, durationMs: 2),
        NodeRunStatus(node: 'generate', running: true),
      ],
      reasoningByNode: const {'hyde': 'AMF is the AMF.'},
      activeNode: 'generate',
      startedAt: DateTime.now().subtract(const Duration(milliseconds: 1500)),
      collapsedFromController: true,
    )));
    expect(find.byKey(const Key('reasoning_panel_collapsed')), findsOneWidget);
    // 折叠态不渲染 chip 列
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsNothing);
    // 「3 步骤」
    expect(find.textContaining('3'), findsOneWidget);

    // 点击切到展开
    await tester.tap(find.byKey(const Key('reasoning_panel_collapsed')));
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);
  });

  testWidgets('用户手动展开后，controller 信号 collapsed=true 不再自动覆盖',
      (tester) async {
    Widget build({required bool collapsedFromController}) =>
        _wrap(ReasoningPanel(
          nodes: const [NodeRunStatus(node: 'hyde', running: true)],
          reasoningByNode: const {'hyde': 'streaming...'},
          activeNode: 'hyde',
          startedAt: DateTime.now(),
          collapsedFromController: collapsedFromController,
        ));

    await tester.pumpWidget(build(collapsedFromController: false));
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);

    // controller 信号切折叠 → 自动折叠
    await tester.pumpWidget(build(collapsedFromController: true));
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('reasoning_panel_collapsed')), findsOneWidget);

    // 用户点折叠条 → 重新展开（_userOverride=false）
    await tester.tap(find.byKey(const Key('reasoning_panel_collapsed')));
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);

    // 即使 controller 仍说 collapsed=true，由于用户已 override 不再自动收起
    await tester.pumpWidget(build(collapsedFromController: true));
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);
  });

  testWidgets('hyde 流式 delta 累积时滚动到底部（不抛异常）', (tester) async {
    Widget build(String hydeText) => _wrap(ReasoningPanel(
          nodes: const [NodeRunStatus(node: 'hyde', running: true)],
          reasoningByNode: {'hyde': hydeText},
          activeNode: 'hyde',
          startedAt: DateTime.now(),
          collapsedFromController: false,
        ));
    await tester.pumpWidget(build('a'));
    await tester.pump();
    await tester.pumpWidget(build('a' * 800)); // 超过 maxHeight=120 触发滚动
    await tester.pump(const Duration(milliseconds: 50));
    // 不抛 + 节点 chip 仍在
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);
  });

  testWidgets('frozenElapsed：折叠态显示固定耗时，不随 rebuild 往上跳，仍可展开复盘',
      (tester) async {
    Widget build() => _wrap(ReasoningPanel(
          nodes: const [
            NodeRunStatus(node: 'classify', running: false, durationMs: 1),
            NodeRunStatus(node: 'hyde', running: false, durationMs: 2),
          ],
          reasoningByNode: const {'hyde': 'AMF is the AMF.'},
          activeNode: null,
          startedAt: null,
          collapsedFromController: true,
          frozenElapsed: const Duration(milliseconds: 4200),
        ));
    await tester.pumpWidget(build());
    // 默认折叠 + 显示冻结的 4.2s
    expect(find.byKey(const Key('reasoning_panel_collapsed')), findsOneWidget);
    expect(find.textContaining('4.2'), findsOneWidget);
    // 多 pump 几帧，秒数保持不变（不像实时计时那样一直累加）
    await tester.pump(const Duration(seconds: 1));
    await tester.pump(const Duration(seconds: 1));
    expect(find.textContaining('4.2'), findsOneWidget);
    // 用户点开 → 仍能展开复盘（chip 列 + 灰色文字区回来）
    await tester.tap(find.byKey(const Key('reasoning_panel_collapsed')));
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.byKey(const Key('reasoning_chip_hyde')), findsOneWidget);
    expect(find.textContaining('AMF is the AMF.'), findsOneWidget);
  });

  test('nodeLabel 返回英文技术名（步骤名不再用中文释义）', () {
    expect(nodeLabel('classify'), 'classify');
    expect(nodeLabel('rewrite'), 'rewrite');
    expect(nodeLabel('hyde'), 'hyde');
    expect(nodeLabel('multi_query'), 'multi_query');
    expect(nodeLabel('self_rag'), 'self_rag');
  });

  testWidgets('formatNodeSummary：覆盖 6 个节点的 summary 渲染', (tester) async {
    late AppLocalizations l;
    await tester.pumpWidget(_wrap(Builder(builder: (ctx) {
      l = AppLocalizations.of(ctx);
      return const SizedBox();
    })));
    // classify 全字段
    expect(
      formatNodeSummary(l, 'classify', {
        'query_class': 'definition',
        'complexity': 'simple',
        'rewritten_query': 'AMF function',
      }),
      contains('definition'),
    );
    // rewrite
    expect(
      formatNodeSummary(l, 'rewrite', {'rewritten_query': 'X'}),
      contains('X'),
    );
    // multi_query
    final mq = formatNodeSummary(l, 'multi_query', {
      'sub_queries': ['A', 'B'],
    });
    expect(mq, contains('A'));
    expect(mq, contains('B'));
    // retrieve
    expect(
      formatNodeSummary(l, 'retrieve', {'candidates_count': 42}),
      contains('42'),
    );
    // rerank
    expect(
      formatNodeSummary(l, 'rerank', {'reranked_count': 8}),
      contains('8'),
    );
    // self_rag
    final sr = formatNodeSummary(l, 'self_rag',
        {'self_rag_verdict': 'accept', 'confidence': 0.87});
    expect(sr, contains('accept'));
    expect(sr, contains('0.87'));
    // 缺字段返回 null
    expect(formatNodeSummary(l, 'rewrite', {}), isNull);
    expect(formatNodeSummary(l, 'multi_query', {}), isNull);
    // 未知节点返回 null
    expect(formatNodeSummary(l, 'unknown_node', {'foo': 1}), isNull);
  });
}
