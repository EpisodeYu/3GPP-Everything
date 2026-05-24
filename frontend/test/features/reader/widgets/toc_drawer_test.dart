import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/docs_api.dart';
import 'package:tgpp/features/reader/widgets/toc_drawer.dart';

import '../../../support/fake_docs_api.dart';

Widget _wrap({required Widget child, required FakeDocsApi docs}) {
  return ProviderScope(
    overrides: [docsApiProvider.overrideWithValue(docs)],
    child: MaterialApp(
      home: Scaffold(body: SizedBox(width: 300, child: child)),
    ),
  );
}

void main() {
  testWidgets('章节树渲染 + 当前节点高亮 + 点击触发回调', (tester) async {
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(
          specId: '23.501',
          sections: [
            buildSectionNode(path: '5.6.1', title: 'PDU Session', chunkCount: 3),
            buildSectionNode(path: '5.6.1.2', title: 'Establishment', chunkCount: 2),
          ],
        ),
      },
    );
    SectionNode? captured;
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: TocDrawer(
        specId: '23.501',
        currentSectionPath: '5.6.1',
        onSelectSection: (n) => captured = n,
        onSelectChunk: (_) {},
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('toc_list')), findsOneWidget);
    expect(find.text('PDU Session'), findsOneWidget);
    expect(find.text('Establishment'), findsOneWidget);
    await tester.tap(find.byKey(const Key('toc_tile_5.6.1.2')));
    await tester.pump();
    expect(captured?.joinedPath, '5.6.1.2');
  });

  testWidgets('搜索框输入 → 切换到搜索结果列表 → 点击触发 onSelectChunk', (tester) async {
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
      searchMap: {
        '23.501/PDU': SearchResponse(
          specId: '23.501',
          query: 'PDU',
          items: [
            buildSearchHit(
              chunkId: 'c-1',
              specId: '23.501',
              sectionPath: '5.6',
              sectionTitle: 'PDU Session',
            ),
          ],
        ),
      },
    );
    SearchHit? captured;
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: TocDrawer(
        specId: '23.501',
        currentSectionPath: null,
        onSelectSection: (_) {},
        onSelectChunk: (h) => captured = h,
      ),
    ));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('toc_search_input')), 'PDU');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('toc_search_list')), findsOneWidget);
    expect(docs.lastSearchedQuery, 'PDU');

    await tester.tap(find.byKey(const Key('toc_search_hit_c-1')));
    await tester.pump();
    expect(captured?.chunkId, 'c-1');
  });

  testWidgets('搜索无结果 → 空提示', (tester) async {
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
    );
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: TocDrawer(
        specId: '23.501',
        currentSectionPath: null,
        onSelectSection: (_) {},
        onSelectChunk: (_) {},
      ),
    ));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('toc_search_input')), 'NOMATCH');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('toc_search_empty')), findsOneWidget);
  });

  testWidgets('章节树加载失败 → 错误占位', (tester) async {
    final docs = FakeDocsApi(); // 没注入 specDetails → 抛 StateError
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: TocDrawer(
        specId: '23.501',
        currentSectionPath: null,
        onSelectSection: (_) {},
        onSelectChunk: (_) {},
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('toc_error')), findsOneWidget);
  });
}
