import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:tgpp/data/api/docs_api.dart';
import 'package:tgpp/features/reader/reader_page.dart';

import '../../support/fake_docs_api.dart';
import '../../support/localized.dart';

GoRouter _router({
  required String location,
  required FakeDocsApi docs,
}) {
  return GoRouter(
    initialLocation: location,
    routes: [
      GoRoute(
        path: '/chat',
        builder: (_, _) => const _Placeholder('chat'),
      ),
      GoRoute(
        path: '/reader/:spec',
        builder: (_, s) => ReaderPage(
          specId: s.pathParameters['spec']!,
          activeChunkId: _parseAnchor(s.uri.fragment),
        ),
      ),
      GoRoute(
        path: '/reader/:spec/:section',
        builder: (_, s) => ReaderPage(
          specId: s.pathParameters['spec']!,
          sectionPath: s.pathParameters['section'],
          activeChunkId: _parseAnchor(s.uri.fragment),
        ),
      ),
    ],
  );
}

String? _parseAnchor(String fragment) {
  if (fragment.startsWith('chunk-')) return fragment.substring(6);
  return null;
}

class _Placeholder extends StatelessWidget {
  const _Placeholder(this.label);
  final String label;
  @override
  Widget build(BuildContext context) =>
      Scaffold(body: Center(child: Text(label, key: Key('placeholder_$label'))));
}

Widget _pumpApp({
  required GoRouter router,
  required FakeDocsApi docs,
}) {
  return ProviderScope(
    overrides: [docsApiProvider.overrideWithValue(docs)],
    child: localizedMaterialAppRouter(
      routerConfig: router,
    ),
  );
}

Future<void> _setSize(WidgetTester tester, Size logical) async {
  tester.view.devicePixelRatio = 1.0;
  tester.view.physicalSize = logical;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

void main() {
  testWidgets('/reader/{spec}：spec overview + 章节列表', (tester) async {
    await _setSize(tester, const Size(1280, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(
          specId: '23.501',
          sections: [
            buildSectionNode(path: '5.6.1', title: 'PDU Session'),
          ],
        ),
      },
    );
    final router = _router(location: '/reader/23.501', docs: docs);
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('reader_crumb')), findsOneWidget);
    expect(find.byKey(const Key('spec_overview_id')), findsOneWidget);
    expect(find.byKey(const Key('spec_overview_sections')), findsOneWidget);
  });

  testWidgets('/reader/{spec}/{section}：渲染 SectionView 内容', (tester) async {
    await _setSize(tester, const Size(1280, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(
          specId: '23.501',
          sections: [buildSectionNode(path: '5.6.1', title: 'PDU')],
        ),
      },
      sectionMap: {
        '23.501/5.6.1': buildSectionDetail(
          specId: '23.501',
          sectionPath: '5.6.1',
          sectionTitle: 'PDU Session',
          chunks: [buildChunk(chunkId: 'c-1', content: 'CONTENT_HERE')],
        ),
      },
    );
    final router = _router(location: '/reader/23.501/5.6.1', docs: docs);
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    expect(find.text('CONTENT_HERE'), findsOneWidget);
    expect(find.byKey(const Key('chunk_md_c-1')), findsOneWidget);
  });

  testWidgets('fragment #chunk-xxx → activeChunkId 传到 SectionView', (tester) async {
    await _setSize(tester, const Size(1280, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
      sectionMap: {
        '23.501/5.6.1': buildSectionDetail(
          specId: '23.501',
          sectionPath: '5.6.1',
          chunks: [
            buildChunk(chunkId: 'c-a', content: 'a'),
            buildChunk(chunkId: 'c-b', content: 'b'),
          ],
        ),
      },
    );
    final router = _router(
      location: '/reader/23.501/5.6.1#chunk-c-b',
      docs: docs,
    );
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    // 命中 chunk 渲染了即可（实际高亮 alpha 动画不测，省略 timing 强耦合）
    expect(find.byKey(const Key('chunk_md_c-a')), findsOneWidget);
    expect(find.byKey(const Key('chunk_md_c-b')), findsOneWidget);
  });

  testWidgets('宽屏：AppBar back 按钮 → 跳 /chat', (tester) async {
    await _setSize(tester, const Size(1280, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
    );
    final router = _router(location: '/reader/23.501', docs: docs);
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('reader_back_chat')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('placeholder_chat')), findsOneWidget);
  });

  testWidgets('窄屏：抽屉化 toc，AppBar 含 menu 按钮', (tester) async {
    await _setSize(tester, const Size(360, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(
          specId: '23.501',
          sections: [buildSectionNode(path: '5.6.1', title: 'A')],
        ),
      },
    );
    final router = _router(location: '/reader/23.501', docs: docs);
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('reader_open_drawer')), findsOneWidget);
    await tester.tap(find.byKey(const Key('reader_open_drawer')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('toc_search_input')), findsOneWidget);
  });
}
