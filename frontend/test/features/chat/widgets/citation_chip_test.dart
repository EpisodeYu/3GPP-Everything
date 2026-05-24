import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:tgpp/data/api/docs_api.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/features/chat/widgets/citation_chip.dart';
import 'package:tgpp/features/chat/widgets/message_bubble.dart';

import '../../../support/fake_docs_api.dart';

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
    child: MaterialApp(home: Scaffold(body: child)),
  );
}

void main() {
  group('CitationInlineSyntax 正则', () {
    test('匹配 [23.501 §5.6.1 ¶3] 并抽出三个 group', () {
      final doc = md.Document(inlineSyntaxes: [CitationInlineSyntax()]);
      final out = doc.parseInline('see [23.501 §5.6.1 ¶3] now');
      final el = out.firstWhere(
        (n) => n is md.Element && n.tag == 'citation',
      ) as md.Element;
      expect(el.attributes['spec'], '23.501');
      expect(el.attributes['section'], '5.6.1');
      expect(el.attributes['rank'], '3');
      expect(el.attributes['raw'], '[23.501 §5.6.1 ¶3]');
    });

    test('不与 markdown link 冲突 [text](url)', () {
      final doc = md.Document(
        inlineSyntaxes: [CitationInlineSyntax()],
        encodeHtml: false,
      );
      final out = doc.parseInline('see [hello](http://x.com)');
      final hasCitation = out.any(
        (n) => n is md.Element && n.tag == 'citation',
      );
      expect(hasCitation, isFalse);
    });

    test('section 单段也匹配（如 §5 ¶1）', () {
      final doc = md.Document(inlineSyntaxes: [CitationInlineSyntax()]);
      final out = doc.parseInline('[23.501 §5 ¶1]');
      final el = out.firstWhere(
        (n) => n is md.Element && n.tag == 'citation',
      ) as md.Element;
      expect(el.attributes['section'], '5');
    });
  });

  group('CitationChip widget', () {
    testWidgets('点击触发 onTap 回调', (tester) async {
      var taps = 0;
      CitationRef? captured;
      final chip = CitationChip(
        ref: const CitationRef(
          specId: '23.501',
          sectionPath: '5.6.1',
          rank: 2,
          rawText: '[23.501 §5.6.1 ¶2]',
          chunkId: 'c-2',
        ),
        onTap: (_, ref) {
          taps += 1;
          captured = ref;
        },
      );
      await tester.pumpWidget(_wrap(child: chip));
      await tester.tap(find.byKey(const Key('citation_chip_23.501_5.6.1_2')));
      await tester.pump();
      expect(taps, 1);
      expect(captured?.chunkId, 'c-2');
    });

    testWidgets('显示 spec/section/rank 文本', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const CitationChip(
          ref: CitationRef(
            specId: '38.331',
            sectionPath: '5.3.5',
            rank: 7,
            rawText: '[38.331 §5.3.5 ¶7]',
          ),
        ),
      ));
      expect(find.text('38.331 §5.3.5 ¶7'), findsOneWidget);
    });

    testWidgets('长按触发 onLongPress 回调（带 ref）', (tester) async {
      CitationRef? captured;
      await tester.pumpWidget(_wrap(
        child: CitationChip(
          ref: const CitationRef(
            specId: '23.501',
            sectionPath: '5.6',
            rank: 1,
            rawText: '[23.501 §5.6 ¶1]',
            chunkId: 'c-1',
          ),
          onLongPress: (_, ref) => captured = ref,
        ),
      ));
      await tester.longPress(find.byKey(const Key('citation_chip_23.501_5.6_1')));
      await tester.pump();
      expect(captured?.specId, '23.501');
      expect(captured?.rawText, '[23.501 §5.6 ¶1]');
    });

    testWidgets('默认长按行为复制 raw 到剪贴板', (tester) async {
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
            specId: '33.501',
            sectionPath: '6.7',
            rank: 4,
            rawText: '[33.501 §6.7 ¶4]',
          ),
        ),
      ));
      await tester.longPress(find.byKey(const Key('citation_chip_33.501_6.7_4')));
      await tester.pumpAndSettle();
      expect(lastSet, '[33.501 §6.7 ¶4]');
    });
  });

  group('MessageBubble 把 [spec §sec ¶rank] 渲染成 chip', () {
    testWidgets('assistant 消息含引用 → chip 渲染', (tester) async {
      await tester.pumpWidget(_wrap(
        child: const MessageBubble(
          role: 'assistant',
          content: 'PDU Session 流程见 [23.501 §5.6.1 ¶0]。',
          citations: [
            MessageCitationOut(
              chunkId: 'c-aaa',
              rank: 0,
              specId: '23.501',
              sectionPath: '5.6.1',
            ),
          ],
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('citation_chip_23.501_5.6.1_0')), findsOneWidget);
    });

    testWidgets('点击 chip 弹出 bottom sheet + 拉 chunk 内容', (tester) async {
      final docs = FakeDocsApi(
        chunkMap: {
          'c-bbb': buildChunk(
            chunkId: 'c-bbb',
            content: 'chunk full text body',
          ),
        },
      );
      await tester.pumpWidget(_wrap(
        docs: docs,
        child: const MessageBubble(
          role: 'assistant',
          content: '看 [23.501 §5.6.1 ¶1]。',
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
      await tester.tap(find.byKey(const Key('citation_chip_23.501_5.6.1_1')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('citation_sheet_title')), findsOneWidget);
      expect(find.text('chunk full text body'), findsOneWidget);
      expect(find.byKey(const Key('citation_sheet_jump')), findsOneWidget);
    });

    testWidgets('chunkId 缺失（citations 没匹配 rank）→ sheet 提示无 chunk', (tester) async {
      await tester.pumpWidget(_wrap(
        docs: FakeDocsApi(),
        child: const MessageBubble(
          role: 'assistant',
          content: '裸引用：[23.501 §5.6.1 ¶9]。',
          citations: [],
        ),
      ));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('citation_chip_23.501_5.6.1_9')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('citation_sheet_no_chunk')), findsOneWidget);
    });
  });
}
