import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/notes_api.dart';
import 'package:tgpp/features/notes/notes_page.dart';

import '../../support/fake_favorites_notes_feedback.dart';
import '../../support/localized.dart';

NoteOut _note(
  String id, {
  String body = '我的笔记',
  String? sessionId = 's-1',
  String? preview = '原消息预览',
}) =>
    NoteOut(
      id: id,
      targetType: 'message',
      targetId: 'm-$id',
      body: body,
      createdAt: DateTime.utc(2026, 5, 24, 20),
      updatedAt: DateTime.utc(2026, 5, 24, 20),
      sessionId: sessionId,
      preview: preview,
    );

Future<FakeNotesApi> _pump(
  WidgetTester tester, {
  required List<NoteOut> items,
}) async {
  final notes = FakeNotesApi(items: items);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [notesApiProvider.overrideWithValue(notes)],
      child: localizedMaterialApp(home: const NotesPage()),
    ),
  );
  await tester.pumpAndSettle();
  return notes;
}

void main() {
  group('NotesPage', () {
    testWidgets('空笔记 → 占位文案', (tester) async {
      await _pump(tester, items: const []);
      expect(find.text('还没有笔记。在回答上长按 → 添加笔记。'), findsOneWidget);
    });

    testWidgets('渲染笔记内容 + 原文预览', (tester) async {
      await _pump(tester, items: [_note('1', body: '记一笔', preview: '某条回答')]);
      expect(find.byKey(const Key('note_tile_1')), findsOneWidget);
      expect(find.text('记一笔'), findsOneWidget);
      expect(find.textContaining('某条回答'), findsOneWidget);
    });

    testWidgets('编辑 → 弹框改内容 → 调 patch', (tester) async {
      final notes = await _pump(tester, items: [_note('1', body: '旧内容')]);
      await tester.tap(find.byKey(const Key('note_edit_1')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('note_edit_field')), findsOneWidget);
      await tester.enterText(find.byKey(const Key('note_edit_field')), '新内容');
      await tester.tap(find.byKey(const Key('note_edit_save')));
      await tester.pumpAndSettle();
      expect(notes.patched['1'], '新内容');
      expect(find.text('新内容'), findsOneWidget);
    });

    testWidgets('编辑弹框取消 → 不调 patch', (tester) async {
      final notes = await _pump(tester, items: [_note('1', body: '旧内容')]);
      await tester.tap(find.byKey(const Key('note_edit_1')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('note_edit_cancel')));
      await tester.pumpAndSettle();
      expect(notes.patched, isEmpty);
    });

    testWidgets('删除 → 调 delete 并移除', (tester) async {
      final notes = await _pump(tester, items: [_note('1'), _note('2')]);
      await tester.tap(find.byKey(const Key('note_delete_1')));
      await tester.pumpAndSettle();
      expect(notes.deletedIds, contains('1'));
      expect(find.byKey(const Key('note_tile_1')), findsNothing);
      expect(find.byKey(const Key('note_tile_2')), findsOneWidget);
    });
  });
}
