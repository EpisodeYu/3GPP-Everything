import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:markdown/markdown.dart' as md;

import '../../../data/api/docs_api.dart';
import '../../../data/api/messages_api.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

/// 解析自 markdown 文本 `[<spec_id> §<section_path> ¶<rank>]` 的引用句柄。
///
/// 来源锚：`docs/03-development/05-frontend.md §5.4`：
/// > chip 与 `message.citations[rank]` 按 `rank` 索引一一对应；
/// > chunk_id 是 Qdrant point id 字符串。
///
/// [chunkId] 可空：当 message 没带 citation 元数据（如裸 markdown 预览 / 历史脏数据），
/// chip 仍能跳转到 spec + section 阅读位（只是 bottom sheet 拉不到 chunk 全文）。
class CitationRef {
  const CitationRef({
    required this.specId,
    required this.sectionPath,
    required this.rank,
    required this.rawText,
    this.chunkId,
  });

  final String specId;
  final String sectionPath;
  final int rank;
  final String? chunkId;

  /// 原始引用文本（含中括号），供长按复制使用。
  final String rawText;
}

/// markdown 内联引用语法：`[23.501 §5.6.1 ¶3]`。
///
/// 设计要点：
/// - 严格正则 `\[\d+\.\d+ §[\d\.]+ ¶\d+\]`，不与 markdown link `[text](url)` 冲突（后者括号内必跟 `(`）
/// - 匹配成功后向 InlineParser 注入 `<citation>` element 节点，由 [CitationElementBuilder] 渲染
/// - `\d+\.\d+` 允许 `23.501` / `38.331` / `33.501` 等 5G 系列编号
class CitationInlineSyntax extends md.InlineSyntax {
  CitationInlineSyntax() : super(_pattern);

  static const String _pattern = r'\[(\d+\.\d+) §([\d\.]+) ¶(\d+)\]';

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    final el = md.Element.empty('citation')
      ..attributes['spec'] = match[1]!
      ..attributes['section'] = match[2]!
      ..attributes['rank'] = match[3]!
      ..attributes['raw'] = match[0]!;
    parser.addNode(el);
    return true;
  }
}

/// `<citation>` element → [CitationChip] widget。
///
/// 调用方在 `MarkdownBody.builders` 里注入：`'citation': CitationElementBuilder(...)`。
class CitationElementBuilder extends MarkdownElementBuilder {
  CitationElementBuilder({
    required this.citationsByRank,
    this.onTap,
    this.onLongPress,
  });

  /// 按 `rank` 查 citation 元数据（拿 chunkId）。message 没 citations → 传空 map。
  final Map<int, MessageCitationOut> citationsByRank;

  /// 点击 chip 触发；默认行为 = 弹 bottom sheet。null 表示交给 [CitationChip] 自处理。
  final void Function(BuildContext context, CitationRef ref)? onTap;

  /// 长按 chip 触发；默认行为 = 复制 raw 文本到剪贴板 + SnackBar。
  final void Function(BuildContext context, CitationRef ref)? onLongPress;

  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final spec = element.attributes['spec'] ?? '';
    final section = element.attributes['section'] ?? '';
    final rank = int.tryParse(element.attributes['rank'] ?? '') ?? 0;
    final raw = element.attributes['raw'] ?? '';
    final citation = citationsByRank[rank];
    final ref = CitationRef(
      specId: spec,
      sectionPath: section,
      rank: rank,
      rawText: raw,
      chunkId: citation?.chunkId,
    );
    return CitationChip(
      ref: ref,
      onTap: onTap,
      onLongPress: onLongPress,
    );
  }
}

/// 引用 chip：圆角胶囊 + 边框，宽度自适应文本。
///
/// 行为锚：`docs/03-development/05-frontend.md §5.4`。
/// - 点击：弹 [showCitationBottomSheet]（拉 `GET /chunks/{chunk_id}` 显示上下文 +
///   "跳到完整章节" 按钮）
/// - 长按：复制原始引用文本到剪贴板
class CitationChip extends ConsumerWidget {
  const CitationChip({
    super.key,
    required this.ref,
    this.onTap,
    this.onLongPress,
  });

  final CitationRef ref;
  final void Function(BuildContext context, CitationRef ref)? onTap;
  final void Function(BuildContext context, CitationRef ref)? onLongPress;

  @override
  Widget build(BuildContext context, WidgetRef wref) {
    final scheme = Theme.of(context).colorScheme;
    final label = '${ref.specId} §${ref.sectionPath} ¶${ref.rank}';
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 2),
      child: Material(
        color: scheme.surfaceContainerHighest,
        shape: StadiumBorder(side: BorderSide(color: scheme.primary)),
        child: InkWell(
          key: Key('citation_chip_${ref.specId}_${ref.sectionPath}_${ref.rank}'),
          customBorder: const StadiumBorder(),
          onTap: () =>
              (onTap ?? _defaultOnTap)(context, ref),
          onLongPress: () =>
              (onLongPress ?? _defaultOnLongPress)(context, ref),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
            child: Text(
              label,
              style: TextStyle(
                color: scheme.primary,
                fontWeight: FontWeight.w500,
                fontSize: 12,
                height: 1.2,
              ),
            ),
          ),
        ),
      ),
    );
  }

  static void _defaultOnTap(BuildContext context, CitationRef ref) {
    showCitationBottomSheet(context, ref);
  }

  static void _defaultOnLongPress(BuildContext context, CitationRef ref) {
    Clipboard.setData(ClipboardData(text: ref.rawText));
    ScaffoldMessenger.maybeOf(context)?.showSnackBar(
      const SnackBar(content: Text('已复制引用文本')),
    );
  }
}

/// 弹底部 sheet：标题 + chunk content（拉 [DocsApi.getChunk]）+ "跳到完整章节" 按钮。
///
/// chunkId 缺失 → 只显示 spec / section 信息和跳转按钮（不拉详情）。
Future<void> showCitationBottomSheet(
  BuildContext context,
  CitationRef ref,
) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (ctx) => _CitationSheet(ref: ref),
  );
}

class _CitationSheet extends ConsumerWidget {
  const _CitationSheet({required this.ref});

  final CitationRef ref;

  @override
  Widget build(BuildContext context, WidgetRef wref) {
    final theme = Theme.of(context);
    final body = SafeArea(
      top: false,
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          16,
          0,
          16,
          16 + MediaQuery.of(context).viewInsets.bottom,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${ref.specId} §${ref.sectionPath} ¶${ref.rank}',
              key: const Key('citation_sheet_title'),
              style: theme.textTheme.titleMedium,
            ),
            const SizedBox(height: 12),
            ConstrainedBox(
              constraints: BoxConstraints(
                maxHeight: MediaQuery.of(context).size.height * 0.5,
              ),
              child: _ContentArea(chunkId: ref.chunkId),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: FilledButton.icon(
                    key: const Key('citation_sheet_jump'),
                    onPressed: () {
                      Navigator.of(context).pop();
                      _jumpToReader(context, ref);
                    },
                    icon: const Icon(Icons.menu_book_outlined),
                    label: const Text('跳到完整章节'),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
    return body;
  }

  static void _jumpToReader(BuildContext context, CitationRef ref) {
    final spec = Uri.encodeComponent(ref.specId);
    final sec = Uri.encodeComponent(ref.sectionPath);
    final fragment = ref.chunkId != null ? '#chunk-${ref.chunkId}' : '';
    GoRouter.of(context).go('/reader/$spec/$sec$fragment');
  }
}

class _ContentArea extends ConsumerWidget {
  const _ContentArea({required this.chunkId});

  final String? chunkId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (chunkId == null || chunkId!.isEmpty) {
      return Text(
        '（无 chunk_id：消息可能来自旧版本或裸文本预览，可点下方按钮直接跳到章节）',
        key: const Key('citation_sheet_no_chunk'),
        style: Theme.of(context).textTheme.bodyMedium,
      );
    }
    final async = ref.watch(_chunkProvider(chunkId!));
    return async.when(
      data: (chunk) => SingleChildScrollView(
        key: const Key('citation_sheet_content'),
        child: SelectableText(
          chunk.content.isEmpty ? '（chunk 内容为空）' : chunk.content,
          style: Theme.of(context).textTheme.bodyMedium,
        ),
      ),
      loading: () => const Padding(
        padding: EdgeInsets.symmetric(vertical: 24),
        child: Center(child: CircularProgressIndicator()),
      ),
      error: (e, _) => Text(
        '加载失败：$e',
        key: const Key('citation_sheet_error'),
        style: TextStyle(color: Theme.of(context).colorScheme.error),
      ),
    );
  }
}

/// 单个 chunk 详情 lazy provider；同一 chunkId 在 sheet 期间共享，
/// sheet 关掉 widget 销毁触发 autoDispose。
final _chunkProvider =
    FutureProvider.autoDispose.family<ChunkOut, String>((ref, chunkId) async {
  final api = ref.watch(docsApiProvider);
  return api.getChunk(chunkId);
});
