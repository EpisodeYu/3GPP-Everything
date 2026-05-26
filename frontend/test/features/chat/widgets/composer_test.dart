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

  // ---------- M5.4 checkpoint UX ----------

  testWidgets('isRunning=true + onPause 提供：暂停 + 取消 双按钮同时存在', (tester) async {
    var paused = 0;
    var cancelled = 0;
    await tester.pumpWidget(_wrap(Composer(
      onSend: (_) {},
      onCancel: () => cancelled++,
      isRunning: true,
      onPause: () => paused++,
    )));
    expect(find.byKey(const Key('composer_pause')), findsOneWidget);
    expect(find.byKey(const Key('composer_cancel')), findsOneWidget);
    expect(find.byKey(const Key('composer_send')), findsNothing);

    await tester.tap(find.byKey(const Key('composer_pause')));
    await tester.pump();
    expect(paused, 1);
    expect(cancelled, 0);

    await tester.tap(find.byKey(const Key('composer_cancel')));
    await tester.pump();
    expect(cancelled, 1);
  });

  testWidgets('isPaused=true：恢复 + 取消 双按钮，输入框禁用 + hint 变化', (tester) async {
    var resumed = 0;
    await tester.pumpWidget(_wrap(Composer(
      onSend: (_) {},
      onCancel: () {},
      isRunning: false,
      isPaused: true,
      onResume: () => resumed++,
    )));
    expect(find.byKey(const Key('composer_resume')), findsOneWidget);
    expect(find.byKey(const Key('composer_cancel')), findsOneWidget);
    expect(find.byKey(const Key('composer_send')), findsNothing);
    expect(find.byKey(const Key('composer_pause')), findsNothing);

    final input = tester.widget<TextField>(find.byKey(const Key('composer_input')));
    expect(input.enabled, false);
    expect(find.text('会话已暂停，点恢复继续'), findsOneWidget);

    await tester.tap(find.byKey(const Key('composer_resume')));
    await tester.pump();
    expect(resumed, 1);
  });

  testWidgets('isRunning=true 但 onPause=null：只显示取消按钮（向后兼容）',
      (tester) async {
    await tester.pumpWidget(_wrap(Composer(
      onSend: (_) {},
      onCancel: () {},
      isRunning: true,
    )));
    expect(find.byKey(const Key('composer_cancel')), findsOneWidget);
    expect(find.byKey(const Key('composer_pause')), findsNothing);
  });
}
