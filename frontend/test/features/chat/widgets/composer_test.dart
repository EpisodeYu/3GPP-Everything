import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/features/chat/widgets/composer.dart';

Widget _wrap(Widget child) =>
    MaterialApp(home: Scaffold(body: Material(child: child)));

void main() {
  testWidgets('空文本不发送，按下发送也是 no-op', (tester) async {
    var sent = 0;
    await tester.pumpWidget(_wrap(Composer(
      onSend: (_) => sent++,
      onCancel: () {},
      isRunning: false,
    )));
    // 按钮禁用：onPressed=null
    final btn = tester.widget<FilledButton>(find.byKey(const Key('composer_send')));
    expect(btn.onPressed, isNull);
    expect(sent, 0);
  });

  testWidgets('输入文本 + 点发送按钮 → onSend 回调（trim 后）', (tester) async {
    final captured = <String>[];
    await tester.pumpWidget(_wrap(Composer(
      onSend: captured.add,
      onCancel: () {},
      isRunning: false,
    )));
    await tester.enterText(find.byKey(const Key('composer_input')), '  hello  ');
    await tester.pump();
    await tester.tap(find.byKey(const Key('composer_send')));
    await tester.pump();
    expect(captured, ['hello']);
    // 发送后输入框清空
    expect(find.text('hello'), findsNothing);
  });

  testWidgets('Enter 发送（无 Shift）', (tester) async {
    final captured = <String>[];
    await tester.pumpWidget(_wrap(Composer(
      onSend: captured.add,
      onCancel: () {},
      isRunning: false,
    )));
    final input = find.byKey(const Key('composer_input'));
    await tester.enterText(input, 'q');
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pump();
    expect(captured, ['q']);
  });

  testWidgets('Shift+Enter 不发送，让输入框换行', (tester) async {
    final captured = <String>[];
    await tester.pumpWidget(_wrap(Composer(
      onSend: captured.add,
      onCancel: () {},
      isRunning: false,
    )));
    final input = find.byKey(const Key('composer_input'));
    await tester.enterText(input, 'q');
    await tester.pump();
    await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
    await tester.pump();
    expect(captured, isEmpty,
        reason: 'Shift+Enter 应交给 TextField 原生换行，不触发 send');
  });

  testWidgets('isRunning=true：按钮变 "取消"，点它走 onCancel', (tester) async {
    var cancelled = 0;
    await tester.pumpWidget(_wrap(Composer(
      onSend: (_) {},
      onCancel: () => cancelled++,
      isRunning: true,
    )));
    expect(find.byKey(const Key('composer_send')), findsNothing);
    expect(find.byKey(const Key('composer_cancel')), findsOneWidget);
    await tester.tap(find.byKey(const Key('composer_cancel')));
    await tester.pump();
    expect(cancelled, 1);
  });

  testWidgets('mode toggle: 点 RawLookup 触发 onModeChanged', (tester) async {
    String? lastMode;
    await tester.pumpWidget(_wrap(Composer(
      onSend: (_) {},
      onCancel: () {},
      isRunning: false,
      mode: 'qa',
      onModeChanged: (m) => lastMode = m,
    )));
    await tester.tap(find.byKey(const Key('composer_mode_raw')));
    await tester.pump();
    expect(lastMode, 'raw_lookup');
  });
}
