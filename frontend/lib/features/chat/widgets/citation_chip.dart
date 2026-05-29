import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:markdown/markdown.dart' as md;

import '../../../data/api/docs_api.dart';
import '../../../data/api/messages_api.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

/// 解析自 markdown 文本 `[N]`（N = 1-based 索引，对应 `message.citations[rank=N]`）
/// 的引用句柄。
///
/// v6（索引引用）锚：`docs/03-development/05-frontend.md §5.4`。
/// LLM 只输出 `[N]`，spec / section / chunk_id / title 等元数据全部从
/// `MessageCitationOut`（后端按 N 精准回填）里读，不再从 inline 文本 parse。
/// 旧 v5 `[spec §section]` 格式 **无 legacy fallback**：旧消息中的引用文本
/// 会按裸文本显示，不渲染 chip（亦不可点）。文本仍可读。
class CitationRef {
  const CitationRef({
    required this.rank,
    required this.rawText,
    this.specId = '',
    this.sectionPath = '',
    this.chunkId,
  });

  /// `[N]` 中的 N（1-based）。
  final int rank;

  /// 来自 `MessageCitationOut.specId`；streaming 期间 citations 尚未到达可能为空。
  final String specId;

  /// 来自 `MessageCitationOut.sectionPath`；streaming 期间或后端没回填可能为空。
  final String sectionPath;

  /// 来自 `MessageCitationOut.chunkId`；null/空时跳转退到 spec 概览页。
  final String? chunkId;

  /// 原始引用文本（含中括号，形如 `[3]`），供长按复制使用。
  final String rawText;
}

/// markdown 内联引用语法：`[N]`（N 为正整数，对齐 v6 索引引用方案）。
///
/// 设计要点：
/// - 正则 `\[(\d+)\]`：仅识别纯数字索引。`[spec §section]`（v5 老格式）/
///   `[link](url)` markdown link 均不匹配，避免误识。
/// - 元数据（spec / section / chunkId）不在 inline 文本里 —— 由
///   [CitationElementBuilder] 用 `rank` 反查 `MessageCitationOut`。
class CitationInlineSyntax extends md.InlineSyntax {
  CitationInlineSyntax() : super(_pattern);

  static const String _pattern = r'\[(\d+)\]';

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    final el = md.Element.empty('citation')
      ..attributes['rank'] = match[1]!
      ..attributes['raw'] = match[0]!;
    parser.addNode(el);
    return true;
  }
}

/// `<citation>` element → [CitationChip] widget。
///
/// 调用方在 `MarkdownBody.builders` 里注入：`'citation': CitationElementBuilder(...)`。
///
/// **缺 citation 兜底**：rank 未在 `citationsByRank` 中（streaming 中途 / 历史消息 /
/// LLM 引用了越界 N 被 backend drop）→ 渲染原始 `[N]` 文本（不出 chip 形态、不可点）。
class CitationElementBuilder extends MarkdownElementBuilder {
  CitationElementBuilder({
    required this.citationsByRank,
    this.onTap,
    this.onLongPress,
  });

  /// 按 `rank` 查 citation 元数据（拿 spec / section / chunkId / title）。
  /// streaming 期间或老消息无 citation → 传空 map / 缺 key → 触发兜底渲染。
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
    final rank = int.tryParse(element.attributes['rank'] ?? '') ?? 0;
    final raw = element.attributes['raw'] ?? '';
    final citation = citationsByRank[rank];
    if (citation == null) {
      // 兜底：rank 找不到对应 citation（streaming 中途 / 老消息 / 越界）。
      // 渲染裸文本，避免出现空 chip 或丢失字符。
      return Text(raw, style: preferredStyle);
    }
    final ref = CitationRef(
      rank: rank,
      rawText: raw,
      specId: citation.specId,
      sectionPath: citation.sectionPath,
      chunkId: citation.chunkId.isEmpty ? null : citation.chunkId,
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
/// - 长按：复制原始引用文本（`[N]`）到剪贴板
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
    final label = ref.sectionPath.isEmpty
        ? ref.specId
        : '${ref.specId} §${ref.sectionPath}';
    final chip = Padding(
      padding: const EdgeInsets.symmetric(horizontal: 2),
      child: Material(
        color: scheme.surfaceContainerHighest,
        shape: StadiumBorder(side: BorderSide(color: scheme.primary)),
        child: InkWell(
          key: Key('citation_chip_${ref.rank}_${ref.specId}_${ref.sectionPath}'),
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
/// v6（索引引用）后 sectionPath 直接来自后端 `MessageCitationOut.sectionPath`，
/// 即 chunks_meta.section_path 的 join('.') 结果——形态稳定（dotted clause 或空串），
/// 不再有 v5 LLM 漂移的 `*ControlResourceSet*` / `—` 占位等。处理：
/// 1. 空 → 跳 spec 概览页 `/reader/{spec}`，SnackBar 提示
/// 2. 合法 dotted clause（`5.3.5.1` / `A.1.2` / `5a` / `5.2.3.2.1_5.3.3_1`）→ 正常跳
/// 3. 其它（极少，理论上不该发生）→ 概览页兜底
void jumpToReader(BuildContext context, CitationRef ref) {
  final spec = Uri.encodeComponent(ref.specId);
  final cleaned = ref.sectionPath.trim();
  final fragment = (ref.chunkId != null && ref.chunkId!.isNotEmpty)
      ? '#chunk-${ref.chunkId}'
      : '';
  final looksLikeClause = cleaned.isNotEmpty &&
      RegExp(r'^[A-Za-z]?[\d][\w.\-]*$').hasMatch(cleaned);
  if (cleaned.isEmpty || !looksLikeClause) {
    final messenger = ScaffoldMessenger.maybeOf(context);
    GoRouter.of(context).go('/reader/$spec$fragment');
    if (messenger != null) {
      final hasChunk = ref.chunkId != null && ref.chunkId!.isNotEmpty;
      final msg = hasChunk
          ? '引用未关联具体章节（IE/ASN.1 chunk），已跳转到规范主页（hover chip 可看 chunk 摘要）'
          : '引用无法定位到具体章节，已跳转到规范主页';
      messenger.showSnackBar(
        SnackBar(
          content: Text(msg),
          duration: const Duration(seconds: 3),
        ),
      );
    }
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
    final header = ref.sectionPath.isEmpty
        ? ref.specId
        : '${ref.specId} §${ref.sectionPath}';
    final headerStyle = theme.textTheme.labelLarge?.copyWith(
      color: theme.colorScheme.onSurface,
      fontWeight: FontWeight.w600,
    );
    final bodyStyle = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onSurfaceVariant,
    );

    if (ref.chunkId == null || ref.chunkId!.isEmpty) {
      // v6 索引方案：parse_citations 必然回填 chunk_id，此分支只剩极端兜底
      // （DB 老数据 / chunk_id 写入失败）。
      return Text(
        '$header（未关联 chunk）',
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
