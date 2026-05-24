import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/docs_api.dart';
import 'package:tgpp/features/reader/widgets/section_view.dart';

import '../../../support/fake_docs_api.dart';

Widget _wrap({required Widget child, required FakeDocsApi docs}) {
  return ProviderScope(
    overrides: [docsApiProvider.overrideWithValue(docs)],
    child: MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 400,
          height: 600,
          child: child,
        ),
      ),
    ),
  );
}

void main() {
  testWidgets('正常加载 → 渲染 section 标题 + 每个 chunk', (tester) async {
    final docs = FakeDocsApi(
      sectionMap: {
        '23.501/5.6.1': buildSectionDetail(
          specId: '23.501',
          sectionPath: '5.6.1',
          sectionTitle: 'PDU Session',
          chunks: [
            buildChunk(chunkId: 'c-1', content: 'first chunk body'),
            buildChunk(chunkId: 'c-2', content: 'second chunk body'),
          ],
        ),
      },
    );
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: const SectionView(specId: '23.501', sectionPath: '5.6.1'),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('section_title')), findsOneWidget);
    expect(find.text('PDU Session'), findsOneWidget);
    expect(find.byKey(const Key('chunk_md_c-1')), findsOneWidget);
    expect(find.byKey(const Key('chunk_md_c-2')), findsOneWidget);
    expect(find.text('first chunk body'), findsOneWidget);
  });

  testWidgets('空 section → 空提示', (tester) async {
    final docs = FakeDocsApi(
      sectionMap: {
        '23.501/9.9.9': buildSectionDetail(
          specId: '23.501',
          sectionPath: '9.9.9',
          chunks: const [],
        ),
      },
    );
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: const SectionView(specId: '23.501', sectionPath: '9.9.9'),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('section_empty')), findsOneWidget);
  });

  testWidgets('section 加载失败 → 错误占位', (tester) async {
    final docs = FakeDocsApi(); // 抛 StateError
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: const SectionView(specId: '23.501', sectionPath: '5.6.1'),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('section_error')), findsOneWidget);
  });

  testWidgets('activeChunkId 命中：渲染时 chunk 也存在（高亮不抛错）', (tester) async {
    final docs = FakeDocsApi(
      sectionMap: {
        '23.501/5.6': buildSectionDetail(
          specId: '23.501',
          sectionPath: '5.6',
          chunks: [
            buildChunk(chunkId: 'c-x', content: 'x'),
            buildChunk(chunkId: 'c-y', content: 'y'),
          ],
        ),
      },
    );
    await tester.pumpWidget(_wrap(
      docs: docs,
      child: const SectionView(
        specId: '23.501',
        sectionPath: '5.6',
        activeChunkId: 'c-y',
      ),
    ));
    await tester.pumpAndSettle();
    // 命中 chunk 也渲染了；不验证高亮颜色（淡出动画 timing 难稳测）
    expect(find.byKey(const Key('chunk_md_c-y')), findsOneWidget);
  });
}
