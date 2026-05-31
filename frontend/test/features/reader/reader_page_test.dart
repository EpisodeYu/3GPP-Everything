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

  testWidgets('深链直接进 reader（无来源页）：返回兜底跳 /chat', (tester) async {
    await _setSize(tester, const Size(1280, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
    );
    final router = _router(location: '/reader/23.501', docs: docs);
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    // 直接深链进入 → 栈里没有来源页 → 返回兜底回会话首页
    await tester.tap(find.byKey(const Key('reader_back_chat')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('placeholder_chat')), findsOneWidget);
  });

  testWidgets('从来源会话 push 进 reader → 返回回到来源会话（不是 /chat）',
      (tester) async {
    await _setSize(tester, const Size(1280, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
    );
    final router = GoRouter(
      initialLocation: '/sessions/s1',
      routes: [
        GoRoute(path: '/chat', builder: (_, _) => const _Placeholder('chat')),
        GoRoute(
          path: '/sessions/:sid',
          builder: (_, _) => Scaffold(
            body: Center(
              child: Builder(
                builder: (ctx) => ElevatedButton(
                  key: const Key('open_reader'),
                  onPressed: () => ctx.push('/reader/23.501'),
                  child: const Text('open'),
                ),
              ),
            ),
          ),
        ),
        GoRoute(
          path: '/reader/:spec',
          builder: (_, s) => ReaderPage(specId: s.pathParameters['spec']!),
        ),
        GoRoute(
          path: '/reader/:spec/:section',
          builder: (_, s) => ReaderPage(
            specId: s.pathParameters['spec']!,
            sectionPath: s.pathParameters['section'],
          ),
        ),
      ],
    );
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('open_reader')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('reader_crumb')), findsOneWidget);

    await tester.tap(find.byKey(const Key('reader_back_chat')));
    await tester.pumpAndSettle();
    // 回到刚才的会话，而不是 /chat
    expect(find.byKey(const Key('open_reader')), findsOneWidget);
    expect(find.byKey(const Key('placeholder_chat')), findsNothing);
  });

  testWidgets('reader 内部切 section（pushReplacement）后返回仍回到来源会话',
      (tester) async {
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
          chunks: [buildChunk(chunkId: 'c1', content: 'SECTION_BODY')],
        ),
      },
    );
    final router = GoRouter(
      initialLocation: '/sessions/s1',
      routes: [
        GoRoute(path: '/chat', builder: (_, _) => const _Placeholder('chat')),
        GoRoute(
          path: '/sessions/:sid',
          builder: (_, _) => Scaffold(
            body: Center(
              child: Builder(
                builder: (ctx) => ElevatedButton(
                  key: const Key('open_reader'),
                  onPressed: () => ctx.push('/reader/23.501'),
                  child: const Text('open'),
                ),
              ),
            ),
          ),
        ),
        GoRoute(
          path: '/reader/:spec',
          builder: (_, s) => ReaderPage(specId: s.pathParameters['spec']!),
        ),
        GoRoute(
          path: '/reader/:spec/:section',
          builder: (_, s) => ReaderPage(
            specId: s.pathParameters['spec']!,
            sectionPath: s.pathParameters['section'],
          ),
        ),
      ],
    );
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const Key('open_reader')));
    await tester.pumpAndSettle();
    // 在 overview 点章节 → pushReplacement 进 section
    await tester.tap(find.text('§5.6.1  PDU'));
    await tester.pumpAndSettle();
    expect(find.text('SECTION_BODY'), findsOneWidget);

    // 返回：跨过被替换的 overview，直接回来源会话
    await tester.tap(find.byKey(const Key('reader_back_chat')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('open_reader')), findsOneWidget);
    expect(find.byKey(const Key('placeholder_chat')), findsNothing);
  });

  testWidgets('窄屏：AppBar 既有返回键也有章节目录键', (tester) async {
    await _setSize(tester, const Size(360, 800));
    final docs = FakeDocsApi(
      specDetails: {
        '23.501': buildDocDetail(specId: '23.501', sections: const []),
      },
    );
    final router = _router(location: '/reader/23.501', docs: docs);
    await tester.pumpWidget(_pumpApp(router: router, docs: docs));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('reader_back_chat')), findsOneWidget);
    expect(find.byKey(const Key('reader_open_drawer')), findsOneWidget);
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
