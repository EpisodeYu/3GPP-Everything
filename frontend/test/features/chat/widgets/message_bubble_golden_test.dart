import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/core/theme.dart';
import 'package:tgpp/data/api/messages_api.dart';
import 'package:tgpp/features/chat/widgets/message_bubble.dart';

import '../../../support/localized.dart';

/// M5.6 golden：聊天气泡 light/dark × zh/en 4 张。
///
/// 锚：`docs/03-development/05-frontend.md §0 M5.6 完成度门禁` + `§11`。
///
/// 目的：把 [MessageBubble] 的视觉契约钉死，未来如果谁动了主题色 / 圆角 / 边距，
/// golden diff 会立刻报警；同时验证 cancelled 状态文案在 zh/en 两套 ARB 下都
/// 渲染正确。
///
/// 截图覆盖：
///   - user 普通气泡（accent 弱填充背景）
///   - assistant 普通气泡（markdown + 引用 chip）
///   - assistant cancelled 气泡（顶部 "已取消 / Cancelled"）
///
/// 仅在 Linux 上生成黄金底片（Flutter golden 在 macOS / web 渲染会有亚像素差），
/// 与 macOS 真机比对会失败 → 用 `--tags=golden` 控制可选执行。CI 与本地 Linux
/// dev box 都用 Linux，差异稳定可忽略。
void main() {
  group('MessageBubble golden (M5.6)', () {
    for (final brightness in [Brightness.light, Brightness.dark]) {
      for (final locale in [const Locale('zh'), const Locale('en')]) {
        final tag = '${brightness.name}_${locale.languageCode}';
        testWidgets('golden $tag', (tester) async {
          await tester.binding.setSurfaceSize(const Size(800, 600));
          addTearDown(() => tester.binding.setSurfaceSize(null));

          await tester.pumpWidget(
            ProviderScope(
              child: MaterialApp(
                theme: brightness == Brightness.light
                    ? AppTheme.light()
                    : AppTheme.dark(),
                locale: locale,
                localizationsDelegates: appLocalizationsDelegates,
                supportedLocales: appSupportedLocales,
                home: Scaffold(
                  body: SingleChildScrollView(
                    child: Column(
                      children: const [
                        SizedBox(height: 12),
                        MessageBubble(
                          role: 'user',
                          content: 'PDU Session 建立流程是什么？',
                        ),
                        MessageBubble(
                          role: 'assistant',
                          content:
                              'PDU Session 建立按 [1] 描述，'
                              'UE 发起 PDU Session Establishment Request。',
                          citations: [
                            MessageCitationOut(
                              chunkId: 'c-gold-1',
                              rank: 1,
                              specId: '23.501',
                              sectionPath: '5.6.1',
                            ),
                          ],
                        ),
                        MessageBubble(
                          role: 'assistant',
                          content: '内容已被用户中途取消。',
                          status: 'cancelled',
                        ),
                        SizedBox(height: 12),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          );
          await tester.pumpAndSettle();

          await expectLater(
            find.byType(MaterialApp),
            matchesGoldenFile('goldens/message_bubble_$tag.png'),
          );
        });
      }
    }
  });
}
