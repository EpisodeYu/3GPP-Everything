import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:tgpp/data/api/docs_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/features/chat/widgets/citation_chip.dart';
import 'package:tgpp/features/chat/widgets/message_bubble.dart';

import '../../../support/fake_docs_api.dart';
import '../../../support/localized.dart';

Widget _wrap({
  required Widget child,
  FakeDocsApi? docs,
  List<Override> overrides = const [],
}) {
  return ProviderScope(
    overrides: [
      if (docs != null) docsApiProvider.overrideWithValue(docs),
      ...overrides,
    ],
    child: localizedMaterialApp(home: Scaffold(body: child)),
  );
}

/// router 版：`/reader/:spec/:section` 落到一个能读出 path 参数的桩页，
/// 用来断言单击 chip 跳转（B3）。
Widget _routerApp({required Widget home, FakeDocsApi? docs}) {
  final router = GoRouter(
    initialLocation: '/',
    routes: [
      GoRoute(path: '/', builder: (_, _) => Scaffold(body: home)),
      GoRoute(
        path: '/reader/:spec/:section',
        builder: (ctx, st) => Scaffold(
          body: Text(
            'READER ${st.pathParameters['spec']} ${st.pathParameters['section']}',
            key: const Key('reader_stub'),
          ),
        ),
      ),
      // spec 概览页：占位章节（非法 section）的跳转兜底落点
      GoRoute(
        path: '/reader/:spec',
        builder: (ctx, st) => Scaffold(
          body: Text(
            'READER ${st.pathParameters['spec']}',
            key: const Key('reader_spec_stub'),
          ),
        ),
      ),
    ],
  );
  return ProviderScope(
    overrides: [if (docs != null) docsApiProvider.overrideWithValue(docs)],
    child: localizedMaterialAppRouter(routerConfig: router),
  );
}

void main() {
  group('CitationInlineSyntax 正则（v6 [N] 索引）', () {
    md.Element? firstCitation(String src) {
      final out = md.Document(inlineSyntaxes: [CitationInlineSyntax()])
          .parseInline(src);
      return out.firstWhereOrNull(
        (n) => n is md.Element && n.tag == 'citation',
      ) as md.Element?;
    }

    test('匹配单个 [1] 并把 rank/raw 写到 attributes', () {
      final el = firstCitation('see [1] now');
      expect(el, isNotNull);
      expect(el!.attributes['rank'], '1');
      expect(el.attributes['raw'], '[1]');
    });

    test('两位数 [42] 也匹配', () {
      final el = firstCitation('see [42] now')!;
      expect(el.attributes['rank'], '42');
    });

    test('连续 [1][2] 分别成两个 citation 元素', () {
      final out = md.Document(inlineSyntaxes: [CitationInlineSyntax()])
          .parseInline('一段 [1][2] 中间');
      final cites = out
          .whereType<md.Element>()
          .where((e) => e.tag == 'citation')
          .toList();
      expect(cites.length, 2);
      expect(cites[0].attributes['rank'], '1');
      expect(cites[1].attributes['rank'], '2');
    });

    test('非连续混排 [1][2] ... [6] ... [8] 全部匹配', () {
      final out = md.Document(inlineSyntaxes: [CitationInlineSyntax()])
          .parseInline('a [1][2] b [6] c [8]');
      final ranks = out
          .whereType<md.Element>()
          .where((e) => e.tag == 'citation')
          .map((e) => e.attributes['rank'])
          .toList();
      expect(ranks, ['1', '2', '6', '8']);
    });

    test('不匹配 markdown link [text](url)', () {
      final out = md.Document(
        inlineSyntaxes: [CitationInlineSyntax()],
        encodeHtml: false,
      ).parseInline('see [hello](http://x.com)');
      expect(
        out.any((n) => n is md.Element && n.tag == 'citation'),
        isFalse,
      );
    });

    test('不匹配 v5 老格式 [spec §section]（不应误识；按裸文本走）', () {
      expect(firstCitation('PRACH 见 [38.213 §8.1]。'), isNull);
      expect(firstCitation('[23.501 §5.6.1 ¶3]'), isNull);
      expect(firstCitation('[38.508-1 § PDSCH-Config]'), isNull);
    });

    test('不匹配 [abc] / [-1] / [] 等非纯正整数', () {
      expect(firstCitation('[abc]'), isNull);
      expect(firstCitation('[-1]'), isNull);
      expect(firstCitation('[]'), isNull);
      expect(firstCitation('[ 1 ]'), isNull);
    });
  });

  group('CitationChip widget', () {
    testWidgets('点击触发 onTap 回调（带 ref）', (tester) async {
      var taps = 0;
      CitationRef? captured;
      await tester.pumpWidget(_wrap(
        child: CitationChip(
          ref: const CitationRef(
            rank: 2,
            rawText: '[2]',
            specId: '23.501',
            sectionPath: '5.6.1',
            chunkId: 'c-2',
          ),
          onTap: (_, r) {
            taps += 1;
            captured = r;
          },
        ),
      ));
      await tester.tap(find.byKey(const Key('citation_chip_2_23.501_5.6.1')));
      await tester.pump();
      expect(taps, 1);
      expect(captured?.chunkId, 'c-2');
      expect(captured?.rank, 2);
    });

    testWidgets('label 展示 "spec §section"（section 非空）', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const CitationChip(
          ref: CitationRef(
            rank: 7,
            rawText: '[7]',
            specId: '38.331',
            sectionPath: '5.3.5',
          ),
        ),
      ));
      expect(find.text('38.331 §5.3.5'), findsOneWidget);
    });

    testWidgets('section 空时 label 只显示 spec', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const CitationChip(
          ref: CitationRef(
            rank: 1,
            rawText: '[1]',
            specId: '38.331',
            sectionPath: '',
            chunkId: 'c-x',
          ),
        ),
      ));
      expect(find.text('38.331'), findsOneWidget);
    });

    testWidgets('默认长按行为：复制 rawText 到剪贴板', (tester) async {
      String? lastSet;
      tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          lastSet = (call.arguments as Map)['text'] as String?;
        }
        return null;
      });
      addTearDown(() {
        tester.binding.defaultBinaryMessenger
            .setMockMethodCallHandler(SystemChannels.platform, null);
      });
      await tester.pumpWidget(_wrap(
        child: const CitationChip(
          ref: CitationRef(
            rank: 4,
            rawText: '[4]',
            specId: '33.501',
            sectionPath: '6.7',
          ),
        ),
      ));
      await tester.longPress(find.byKey(const Key('citation_chip_4_33.501_6.7')));
      await tester.pumpAndSettle();
      expect(lastSet, '[4]');
    });
  });

  group('MessageBubble 把 [N] 渲染成 chip（v6 索引方案）', () {
    testWidgets('citationsByRank 命中 → 渲染 chip', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const MessageBubble(
          role: 'assistant',
          content: 'PDU Session 流程见 [1]。',
          citations: [
            MessageCitationOut(
              chunkId: 'c-aaa',
              rank: 1,
              specId: '23.501',
              sectionPath: '5.6.1',
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('citation_chip_1_23.501_5.6.1')),
        findsOneWidget,
      );
    });

    testWidgets('citationsByRank 缺失 → 渲染裸 [N] 文本（不出 chip）', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const MessageBubble(
          role: 'assistant',
          content: '见 [99]。',
          citations: [],
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.byType(CitationChip), findsNothing);
      expect(find.textContaining('[99]'), findsOneWidget);
    });

    // v6 索引引用回归（2026-05-29）：非连续 rank [1][2][6][8] 必须全部 chip 化，
    // 不再因为 _flushDoneToHistory 用 loop 索引而错位 / 丢失。
    testWidgets('LLM 输出 [1][2]...[6]...[8] 非连续引用全部 chip 化且 label 取对', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const MessageBubble(
          role: 'assistant',
          content: 'DRX [1][2] 工作原理 [6] 应用 [8]。',
          citations: [
            MessageCitationOut(
              chunkId: 'c-1', rank: 1, specId: '38.321', sectionPath: '5.7'),
            MessageCitationOut(
              chunkId: 'c-2', rank: 2, specId: '38.321', sectionPath: '5.7'),
            MessageCitationOut(
              chunkId: 'c-6', rank: 6, specId: '38.321', sectionPath: '5.7.3'),
            MessageCitationOut(
              chunkId: 'c-8', rank: 8, specId: '36.321', sectionPath: '5.5'),
          ],
        ),
      ));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('citation_chip_1_38.321_5.7')), findsOneWidget);
      expect(
        find.byKey(const Key('citation_chip_2_38.321_5.7')), findsOneWidget);
      expect(
        find.byKey(const Key('citation_chip_6_38.321_5.7.3')), findsOneWidget);
      expect(
        find.byKey(const Key('citation_chip_8_36.321_5.5')), findsOneWidget);
      // label 也跟着 rank 走（[6] 取的是 38.321 §5.7.3，不是错位的 §5.7）
      expect(find.text('38.321 §5.7.3'), findsOneWidget);
      expect(find.text('36.321 §5.5'), findsOneWidget);
    });

    testWidgets('单击 chip 直跳 reader（带 chunk 锚点）', (tester) async {
      await tester.pumpWidget(_routerApp(
        home: const MessageBubble(
          role: 'assistant',
          content: '看 [1]。',
          citations: [
            MessageCitationOut(
              chunkId: 'c-bbb',
              rank: 1,
              specId: '23.501',
              sectionPath: '5.6.1',
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const Key('citation_chip_1_23.501_5.6.1')),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('reader_stub')), findsOneWidget);
      expect(find.text('READER 23.501 5.6.1'), findsOneWidget);
    });

    testWidgets('citation 含非法 section（含 * / 空格）→ 单击落到 spec 概览页 + SnackBar', (tester) async {
      await tester.pumpWidget(_routerApp(
        home: const MessageBubble(
          role: 'assistant',
          content: '见 [1]。',
          citations: [
            MessageCitationOut(
              chunkId: 'c-cors-1',
              rank: 1,
              specId: '38.331',
              sectionPath: '*ControlResourceSet* information element',
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const Key(
            'citation_chip_1_38.331_*ControlResourceSet* information element')),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byKey(const Key('reader_spec_stub')), findsOneWidget);
      expect(find.text('READER 38.331'), findsOneWidget);
      expect(find.textContaining('hover chip'), findsOneWidget);
    });

    testWidgets('citation 空 section + 无 chunkId → 落到 spec 概览 + 提示规范主页', (tester) async {
      await tester.pumpWidget(_routerApp(
        home: const MessageBubble(
          role: 'assistant',
          content: '见 [1]。',
          citations: [
            MessageCitationOut(
              chunkId: '',
              rank: 1,
              specId: '38.331',
              sectionPath: '',
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('citation_chip_1_38.331_')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byKey(const Key('reader_spec_stub')), findsOneWidget);
      expect(find.textContaining('规范主页'), findsOneWidget);
    });
  });
}

extension _FirstWhereOrNull<E> on Iterable<E> {
  E? firstWhereOrNull(bool Function(E) test) {
    for (final e in this) {
      if (test(e)) return e;
    }
    return null;
  }
}
