import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/favorites_api.dart';
import 'package:tgpp/features/favorites/favorites_page.dart';

import '../../support/fake_favorites_notes_feedback.dart';
import '../../support/localized.dart';

FavoriteOut _fav(String id, {String? sessionId = 's-1', String? preview = '预览内容'}) =>
    FavoriteOut(
      id: id,
      targetType: 'message',
      targetId: 'm-$id',
      createdAt: DateTime.utc(2026, 5, 24, 20),
      sessionId: sessionId,
      preview: preview,
    );

Future<FakeFavoritesApi> _pump(
  WidgetTester tester, {
  required List<FavoriteOut> items,
}) async {
  final fav = FakeFavoritesApi(items: items);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [favoritesApiProvider.overrideWithValue(fav)],
      child: localizedMaterialApp(home: const FavoritesPage()),
    ),
  );
  await tester.pumpAndSettle();
  return fav;
}

void main() {
  group('FavoritesPage', () {
    testWidgets('空收藏 → 占位文案', (tester) async {
      await _pump(tester, items: const []);
      expect(find.text('还没有收藏。在回答上长按 → 收藏。'), findsOneWidget);
    });

    testWidgets('渲染收藏列表 + 内容预览', (tester) async {
      await _pump(tester, items: [
        _fav('1', preview: 'NR PDCP 详解'),
        _fav('2', preview: '另一条收藏'),
      ]);
      expect(find.byKey(const Key('favorite_tile_1')), findsOneWidget);
      expect(find.byKey(const Key('favorite_tile_2')), findsOneWidget);
      expect(find.text('NR PDCP 详解'), findsOneWidget);
    });

    testWidgets('原消息已删除（preview=null）→ 兜底文案', (tester) async {
      await _pump(tester, items: [_fav('1', sessionId: null, preview: null)]);
      expect(find.text('（原消息已删除）'), findsOneWidget);
    });

    testWidgets('删除 → 调 delete 并从列表移除', (tester) async {
      final fav = await _pump(tester, items: [_fav('1'), _fav('2')]);
      await tester.tap(find.byKey(const Key('favorite_delete_1')));
      await tester.pumpAndSettle();
      expect(fav.deletedIds, contains('1'));
      expect(find.byKey(const Key('favorite_tile_1')), findsNothing);
      expect(find.byKey(const Key('favorite_tile_2')), findsOneWidget);
    });
  });
}
