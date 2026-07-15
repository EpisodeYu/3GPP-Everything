import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/markdown_style.dart';
import '../../../data/api/docs_api.dart';
import '../reader_controller.dart';
import 'highlight_overlay.dart';

/// 单个 section 的内容渲染区。
///
/// 行为锚：`docs/03-development/05-frontend.md §6`：
/// - 标题 + 路径 + 每个 chunk 一个 markdown 块（按 `chunk_type` 简易区分前缀）
/// - 锚点 `#chunk-{chunk_id}` 由 [activeChunkId] 触发：自动 ensureVisible +
///   3s 高亮淡出（[HighlightOverlay]）
/// - chunks 加载 / 失败 / 空 三态显示
class SectionView extends ConsumerStatefulWidget {
  const SectionView({
    super.key,
    required this.specId,
    required this.sectionPath,
    this.activeChunkId,
  });

  final String specId;
  final String sectionPath;

  /// 来自 URL fragment `#chunk-xxx` 的 chunk id；命中时高亮 + 滚到可见。
  final String? activeChunkId;

  @override
  ConsumerState<SectionView> createState() => _SectionViewState();
}

class _SectionViewState extends ConsumerState<SectionView> {
  final Map<String, GlobalKey> _chunkKeys = {};
  String? _lastTriggeredId;

  GlobalKey _keyFor(String chunkId) =>
      _chunkKeys.putIfAbsent(chunkId, () => GlobalKey());

  void _maybeScrollToActive() {
    final id = widget.activeChunkId;
    if (id == null || id.isEmpty) return;
    if (id == _lastTriggeredId) return;
    final key = _chunkKeys[id];
    if (key == null) return;
    final ctx = key.currentContext;
    if (ctx == null) return;
    _lastTriggeredId = id;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      Scrollable.ensureVisible(
        ctx,
        duration: const Duration(milliseconds: 350),
        curve: Curves.easeOut,
        alignment: 0.1,
      );
    });
  }

  @override
  void didUpdateWidget(covariant SectionView old) {
    super.didUpdateWidget(old);
    if (widget.activeChunkId != old.activeChunkId) {
      _lastTriggeredId = null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final ref = SectionRef(
      specId: widget.specId,
      sectionPath: widget.sectionPath,
    );
    final async = this.ref.watch(sectionDetailProvider(ref));
    return async.when(
      loading: () => const Center(
        key: Key('section_loading'),
        child: CircularProgressIndicator(),
      ),
      error: (e, _) => Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            '加载 section 失败：$e',
            key: const Key('section_error'),
            textAlign: TextAlign.center,
          ),
        ),
      ),
      data: (resp) {
        if (resp.chunks.isEmpty) {
          return Center(
            child: Text(
              '该 section 没有 chunk。',
              key: const Key('section_empty'),
              style: Theme.of(context).textTheme.bodyMedium,
            ),
          );
        }
        _maybeScrollToActive();
        return _ChunksList(
          resp: resp,
          activeChunkId: widget.activeChunkId,
          chunkKeyOf: _keyFor,
        );
      },
    );
  }
}

class _ChunksList extends StatelessWidget {
  const _ChunksList({
    required this.resp,
    required this.activeChunkId,
    required this.chunkKeyOf,
  });

  final SectionDetailResponse resp;
  final String? activeChunkId;
  final GlobalKey Function(String) chunkKeyOf;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListView(
      key: const Key('section_chunks_list'),
      padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
      children: [
        Text(
          '${resp.specId} §${resp.joinedPath}',
          style: theme.textTheme.bodySmall?.copyWith(
            fontFamily: 'monospace',
            color: theme.colorScheme.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          resp.sectionTitle.isEmpty ? '(untitled)' : resp.sectionTitle,
          key: const Key('section_title'),
          style: theme.textTheme.headlineSmall,
        ),
        const SizedBox(height: 16),
        for (final c in resp.chunks)
          _ChunkBlock(
            key: chunkKeyOf(c.chunkId),
            chunk: c,
            active: c.chunkId == activeChunkId,
          ),
      ],
    );
  }
}

class _ChunkBlock extends StatelessWidget {
  const _ChunkBlock({super.key, required this.chunk, required this.active});

  final ChunkOut chunk;
  final bool active;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final content = chunk.content.isEmpty ? '(空 chunk)' : chunk.content;
    return Padding(
      padding: const EdgeInsets.only(bottom: 18),
      child: HighlightOverlay(
        active: active,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                _ChunkTypeBadge(type: chunk.chunkType),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'chunk_id=${chunk.chunkId}',
                    style: theme.textTheme.bodySmall?.copyWith(
                      fontFamily: 'monospace',
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 6),
            MarkdownBody(
              key: Key('chunk_md_${chunk.chunkId}'),
              data: content,
              selectable: true,
              shrinkWrap: true,
              styleSheet: appMarkdownStyleSheet(context),
            ),
          ],
        ),
      ),
    );
  }
}

class _ChunkTypeBadge extends StatelessWidget {
  const _ChunkTypeBadge({required this.type});
  final String type;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHigh,
        border: Border.all(color: scheme.outlineVariant),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(
        type,
        style: theme.textTheme.labelSmall?.copyWith(
          color: scheme.onSurfaceVariant,
          fontFamily: 'monospace',
        ),
      ),
    );
  }
}
