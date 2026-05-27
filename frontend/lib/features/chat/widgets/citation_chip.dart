import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:markdown/markdown.dart' as md;

import '../../../data/api/docs_api.dart';
import '../../../data/api/messages_api.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

/// 解析自 markdown 文本 `[<spec_id> §<section_path>( ¶<rank>)?]` 的引用句柄。
///
/// 来源锚：`docs/03-development/05-frontend.md §5.4`：
/// > chip 与 `message.citations[rank]` 按 `rank` 索引一一对应；
/// > chunk_id 是 Qdrant point id 字符串。
///
/// [rank] 可缺省：generate 节点的 LLM 实际只稳定输出 `[spec §section]`（无 `¶rank`），
/// 此时 rank 取哨兵值 `0`，chip 仍按 spec+section 跳转，只是 hover 预览拿不到对应 chunk。
///
/// [chunkId] 可空：当 message 没带 citation 元数据（如裸 markdown 预览 / 历史脏数据），
/// chip 仍能跳转到 spec + section 阅读位（只是 hover 预览拉不到 chunk 全文）。
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

/// markdown 内联引用语法：`[23.501 §5.6.1 ¶3]` / `[38.213 §8.1]`（无 ¶rank）/
/// `[36.523-1 §7.1.6.2.2]`（多部分 spec 带 `-N` 后缀）。
///
/// 设计要点：
/// - 正则 `\[(\d+\.\d+(-\d+)?) §\s*([^\]¶]+?)( ¶\d+)?\]`，`¶rank` 整段可选——后端
///   generate 的 LLM 实际只稳定输出 `[spec §section]`，强制三段会让引用退化成普通文本。
/// - section 段刻意放宽到"非 `]`/`¶` 的任意串"（先前 `[\d\.]+` 只认数字+点）：实测
///   LLM 会输出 `§ 5.2.3.2.1_5.3.3_1`（下划线复合章节）、`§ PDSCH-Config`（IE 名当章节）、
///   `§ —`（章节未知时的破折号占位），且 `§` 后常多打一个空格。窄正则会让这些整条退化成
///   裸文本（chip 不渲染 = "超链接失效"）。放宽后至少渲染出 chip；占位/IE 名这类无效目标的
///   跳转兜底见 [jumpToReader]。根治仍靠 prompt 收紧 LLM 的引用格式。
/// - 不与 markdown link `[text](url)` 冲突：仍要求 `[` 后紧跟 spec 号且串内含 ` §`。
/// - spec 号 `\d+\.\d+(-\d+)?` 允许 `23.501` / `38.331`，以及多部分测试规范
///   `36.523-1` / `38.523-2`（`-N` 后缀不可漏，否则整条引用不渲染）。
class CitationInlineSyntax extends md.InlineSyntax {
  CitationInlineSyntax() : super(_pattern);

  static const String _pattern =
      r'\[(\d+\.\d+(?:-\d+)?) §\s*([^\]¶]+?)(?: ¶(\d+))?\]';

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    final el = md.Element.empty('citation')
      ..attributes['spec'] = match[1]!
      // section 段可能带 § 后空格或末尾空白（`§ 5.6.1 ` / `§ — PDSCH-Config`）→ trim
      ..attributes['section'] = match[2]!.trim()
      // group 3（¶rank）可能未捕获 → 缺省哨兵 '0'
      ..attributes['rank'] = match[3] ?? '0'
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

  /// 点击 chip 触发；默认行为 = 直跳 reader。null 表示交给 [CitationChip] 自处理。
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

/// 引用 chip：圆角胶囊 + 主色边框/文字，宽度自适应文本。
///
/// 行为锚：`docs/03-development/05-frontend.md §5.4`（B3 决策）。
/// - 单击：直接跳 reader `/reader/{spec}/{section}`（chunkId 在则带 `#chunk-` 锚点），
///   不再弹 bottom sheet
/// - hover：弹 [Tooltip] 预览对应 chunk 上下文（拉 `GET /chunks/{chunk_id}`）；
///   缺 chunkId 时只显示 "spec §section（无 chunk 上下文）"
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
    // rank 为哨兵 0（无 ¶）时不展示 "¶0"，只显示 spec §section。
    final label = ref.rank > 0
        ? '${ref.specId} §${ref.sectionPath} ¶${ref.rank}'
        : '${ref.specId} §${ref.sectionPath}';
    final chip = Padding(
      padding: const EdgeInsets.symmetric(horizontal: 2),
      child: Material(
        color: scheme.surfaceContainerHighest,
        shape: StadiumBorder(side: BorderSide(color: scheme.primary)),
        child: InkWell(
          key: Key('citation_chip_${ref.specId}_${ref.sectionPath}_${ref.rank}'),
          customBorder: const StadiumBorder(),
          onTap: () => (onTap ?? _defaultOnTap)(context, ref),
          onLongPress: () => (onLongPress ?? _defaultOnLongPress)(context, ref),
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
    // hover 预览：waitDuration 300ms 充当 debounce，避免划过即触发 chunk 拉取。
    // triggerMode=manual：禁掉触摸端 long-press 触发 tooltip，把长按让给 InkWell
    // 的"复制引用文本"（C1）；hover 仍由 Tooltip 内部 MouseRegion 独立处理。
    return Tooltip(
      triggerMode: TooltipTriggerMode.manual,
      waitDuration: const Duration(milliseconds: 300),
      padding: const EdgeInsets.all(10),
      margin: const EdgeInsets.symmetric(horizontal: 12),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: scheme.outlineVariant),
      ),
      richMessage: WidgetSpan(
        alignment: PlaceholderAlignment.baseline,
        baseline: TextBaseline.alphabetic,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 360),
          child: _CitationPreview(ref: ref),
        ),
      ),
      child: chip,
    );
  }

  /// 单击默认行为（B3）：直跳 reader，不弹 sheet。
  static void _defaultOnTap(BuildContext context, CitationRef ref) {
    jumpToReader(context, ref);
  }

  static void _defaultOnLongPress(BuildContext context, CitationRef ref) {
    Clipboard.setData(ClipboardData(text: ref.rawText));
    ScaffoldMessenger.maybeOf(context)?.showSnackBar(
      const SnackBar(content: Text('已复制引用文本')),
    );
  }
}

/// 跳到 reader 对应章节；chunkId 在则带 `#chunk-` fragment 做锚点定位。
///
/// section 放宽后可能是破折号占位（`§ —` → sectionPath `—`）等无可跳转目标的串：
/// 剥掉前导破折号/空白后若为空，落到 spec 概览页 `/reader/{spec}`（ReaderPage 在
/// sectionPath 为空时渲染 _SpecOverview），避免跳进一个加载不出的空章节。
void jumpToReader(BuildContext context, CitationRef ref) {
  final spec = Uri.encodeComponent(ref.specId);
  final cleaned = ref.sectionPath.replaceFirst(RegExp(r'^[\s—–-]+'), '').trim();
  final fragment = (ref.chunkId != null && ref.chunkId!.isNotEmpty)
      ? '#chunk-${ref.chunkId}'
      : '';
  if (cleaned.isEmpty) {
    GoRouter.of(context).go('/reader/$spec$fragment');
    return;
  }
  final sec = Uri.encodeComponent(cleaned);
  GoRouter.of(context).go('/reader/$spec/$sec$fragment');
}

/// hover tooltip 内容：标题（spec §section）+ chunk 预览正文。
///
/// chunkId 缺失 → 只显示标题 + "无 chunk 上下文"；否则 lazy 拉 [DocsApi.getChunk]。
/// 只在 tooltip 真正展示（hover 300ms 后）时才 mount，所以拉取是按需触发的。
class _CitationPreview extends ConsumerWidget {
  const _CitationPreview({required this.ref});

  final CitationRef ref;

  @override
  Widget build(BuildContext context, WidgetRef wref) {
    final theme = Theme.of(context);
    final header = '${ref.specId} §${ref.sectionPath}';
    final headerStyle = theme.textTheme.labelLarge?.copyWith(
      color: theme.colorScheme.onSurface,
      fontWeight: FontWeight.w600,
    );
    final bodyStyle = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onSurfaceVariant,
    );

    if (ref.chunkId == null || ref.chunkId!.isEmpty) {
      return Text(
        '$header（无 chunk 上下文）',
        key: const Key('citation_preview_no_chunk'),
        style: headerStyle,
      );
    }

    final async = wref.watch(_chunkProvider(ref.chunkId!));
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(header, style: headerStyle),
        const SizedBox(height: 4),
        async.when(
          data: (chunk) => Text(
            chunk.content.isEmpty ? '（chunk 内容为空）' : chunk.content,
            key: const Key('citation_preview_content'),
            maxLines: 8,
            overflow: TextOverflow.ellipsis,
            style: bodyStyle,
          ),
          loading: () => Text('加载预览…', style: bodyStyle),
          error: (e, _) => Text('（预览加载失败）', style: bodyStyle),
        ),
      ],
    );
  }
}

/// 单个 chunk 详情 lazy provider；tooltip 展示期间共享，hover 离开 widget 销毁触发
/// autoDispose。
final _chunkProvider =
    FutureProvider.autoDispose.family<ChunkOut, String>((ref, chunkId) async {
  final api = ref.watch(docsApiProvider);
  return api.getChunk(chunkId);
});
