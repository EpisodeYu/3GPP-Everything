import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/sessions_api.dart';
import 'package:tgpp/domain/session/sessions_controller.dart';

import '../../support/fake_sessions_api.dart';

ProviderContainer _container(FakeSessionsApi api) {
  final container = ProviderContainer(
    overrides: [
      sessionsApiProvider.overrideWithValue(api),
    ],
  );
  addTearDown(container.dispose);
  return container;
}

void main() {
  group('SessionsController', () {
    test('build() 首次调用 API.list 拉到所有 session', () async {
      final api = FakeSessionsApi(initial: [
        buildSession(id: 'a', title: 'A'),
        buildSession(id: 'b'),
      ]);
      final container = _container(api);

      final items = await container.read(sessionsControllerProvider.future);

      expect(items.length, 2);
      expect(items.map((e) => e.id), ['a', 'b']);
      expect(api.listCalls, 1);
    });

    test('createBlank 成功后插入列表头部并返回新会话', () async {
      final api = FakeSessionsApi(initial: [
        buildSession(id: 'a', title: 'A'),
      ]);
      final container = _container(api);
      await container.read(sessionsControllerProvider.future);

      final created = await container
          .read(sessionsControllerProvider.notifier)
          .createBlank(title: 'fresh');

      expect(created.title, 'fresh');
      final items = container.read(sessionsControllerProvider).value!;
      expect(items.first.id, created.id);
      expect(items.length, 2);
    });

    test('delete 乐观移除，成功后保持移除态', () async {
      final api = FakeSessionsApi(initial: [
        buildSession(id: 'a', title: 'A'),
        buildSession(id: 'b', title: 'B'),
      ]);
      final container = _container(api);
      await container.read(sessionsControllerProvider.future);

      await container.read(sessionsControllerProvider.notifier).delete('a');

      final items = container.read(sessionsControllerProvider).value!;
      expect(items.map((e) => e.id), ['b']);
      expect(api.deleteCalls, 1);
    });

    test('delete 失败回滚到原列表，并把异常 rethrow', () async {
      final api = FakeSessionsApi(initial: [
        buildSession(id: 'a'),
        buildSession(id: 'b'),
      ]);
      final container = _container(api);
      await container.read(sessionsControllerProvider.future);
      // build 已用掉一次 list 调用；delete 才该失败
      api.failNext = true;

      await expectLater(
        container.read(sessionsControllerProvider.notifier).delete('a'),
        throwsA(isA<SessionsApiFakeError>()),
      );
      final items = container.read(sessionsControllerProvider).value!;
      expect(items.map((e) => e.id), ['a', 'b']);
    });

    test('rename 成功后替换该 session 的 title，顺序不变', () async {
      final api = FakeSessionsApi(initial: [
        buildSession(id: 'a', title: '旧'),
        buildSession(id: 'b', title: 'B'),
      ]);
      final container = _container(api);
      await container.read(sessionsControllerProvider.future);

      await container
          .read(sessionsControllerProvider.notifier)
          .rename('a', '新标题');

      final items = container.read(sessionsControllerProvider).value!;
      expect(items[0].id, 'a');
      expect(items[0].title, '新标题');
      expect(items[1].id, 'b');
    });
  });
}
