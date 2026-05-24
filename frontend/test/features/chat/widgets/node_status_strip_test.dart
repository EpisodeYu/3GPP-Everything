import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/features/chat/chat_controller.dart';
import 'package:tgpp/features/chat/widgets/node_status_strip.dart';

Widget _wrap(Widget child) =>
    MaterialApp(home: Scaffold(body: Material(child: child)));

void main() {
  testWidgets('空 nodes 不渲染任何 chip', (tester) async {
    await tester.pumpWidget(_wrap(const NodeStatusStrip(nodes: [])));
    expect(find.byType(InputChip), findsNothing);
  });

  testWidgets('running / done 节点都渲染，done 带 duration 文本', (tester) async {
    await tester.pumpWidget(_wrap(const NodeStatusStrip(nodes: [
      NodeRunStatus(node: 'classify', running: false, durationMs: 12, summary: {'query_class': 'kpi'}),
      NodeRunStatus(node: 'retrieve', running: true),
    ])));
    expect(find.byKey(const Key('node_chip_classify')), findsOneWidget);
    expect(find.byKey(const Key('node_chip_retrieve')), findsOneWidget);
    expect(find.textContaining('12ms'), findsOneWidget);
  });

  testWidgets('点 chip 打开 bottom sheet 显示 summary key', (tester) async {
    await tester.pumpWidget(_wrap(const NodeStatusStrip(nodes: [
      NodeRunStatus(
        node: 'rerank',
        running: false,
        durationMs: 5,
        summary: {'reranked_count': 7},
      ),
    ])));
    await tester.tap(find.byKey(const Key('node_chip_rerank')));
    await tester.pumpAndSettle();
    expect(find.text('reranked_count: 7'), findsOneWidget);
  });
}
