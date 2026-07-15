import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

/// 统一的 Markdown 排版样式，供聊天气泡（[MessageBubble]）与阅读器
/// （[SectionView]）共用，保证答案 / 规范正文这些「主内容」在两个主题下
/// 都清晰、有层次、代码块 / 引用 / 表格可读。
///
/// 只调排版（字号跟随 [TextTheme]、代码用 monospace + 浅底块、引用左描边、
/// 表格发丝边），不改 markdown 的解析行为。
MarkdownStyleSheet appMarkdownStyleSheet(BuildContext context) {
  final theme = Theme.of(context);
  final scheme = theme.colorScheme;
  final text = theme.textTheme;
  final codeBg = scheme.surfaceContainerHigh;
  final codeSize = (text.bodyMedium?.fontSize ?? 14) * 0.92;

  return MarkdownStyleSheet.fromTheme(theme).copyWith(
    p: text.bodyMedium,
    a: text.bodyMedium?.copyWith(
      color: scheme.primary,
      decoration: TextDecoration.underline,
      decorationColor: scheme.primary.withValues(alpha: 0.5),
    ),
    h1: text.headlineSmall,
    h2: text.titleLarge,
    h3: text.titleMedium,
    h4: text.titleSmall,
    h5: text.labelLarge,
    h6: text.labelLarge?.copyWith(color: scheme.onSurfaceVariant),
    listBullet: text.bodyMedium,
    blockSpacing: 10,
    code: TextStyle(
      fontFamily: 'monospace',
      fontSize: codeSize,
      height: 1.4,
      color: scheme.onSurface,
      backgroundColor: codeBg,
    ),
    codeblockPadding: const EdgeInsets.all(12),
    codeblockDecoration: BoxDecoration(
      color: codeBg,
      borderRadius: BorderRadius.circular(10),
      border: Border.all(color: scheme.outlineVariant),
    ),
    blockquotePadding: const EdgeInsets.fromLTRB(14, 8, 14, 8),
    blockquoteDecoration: BoxDecoration(
      color: scheme.surfaceContainer,
      borderRadius: BorderRadius.circular(8),
      border: Border(
        left: BorderSide(
          color: scheme.primary.withValues(alpha: 0.5),
          width: 3,
        ),
      ),
    ),
    tableHead: text.labelLarge,
    tableBody: text.bodySmall?.copyWith(color: scheme.onSurface),
    tableBorder: TableBorder.all(color: scheme.outlineVariant),
    tableCellsPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
    horizontalRuleDecoration: BoxDecoration(
      border: Border(top: BorderSide(color: scheme.outlineVariant)),
    ),
  );
}
