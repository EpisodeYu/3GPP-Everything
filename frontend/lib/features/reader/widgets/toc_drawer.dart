import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/api/docs_api.dart';
import '../reader_controller.dart';

/// Reader 左抽屉：spec metadata 头 + 搜索框 + 章节树 / 搜索结果列表。
///
/// 行为锚：`docs/03-development/05-frontend.md §6`。
/// - 搜索框非空 → 渲染搜索结果（点击跳到 section + 高亮 chunk）
/// - 搜索框为空 → 渲染章节树（点击跳到 section）
class TocDrawer extends ConsumerStatefulWidget {
  const TocDrawer({
    super.key,
    required this.specId,
    required this.currentSectionPath,
    required this.onSelectSection,
    required this.onSelectChunk,
  });

  final String specId;

  /// 当前路由命中的 section path（高亮当前节点用）；null 表示在 spec 首页。
  final String? currentSectionPath;

  /// 点击章节树节点时回调，参数是 `[5, 6, 1, 2]` 风格 path。
  final void Function(SectionNode node) onSelectSection;

  /// 点击搜索结果时回调，参数是 hit（含 chunkId）。
  final void Function(SearchHit hit) onSelectChunk;

  @override
  ConsumerState<TocDrawer> createState() => _TocDrawerState();
}

class _TocDrawerState extends ConsumerState<TocDrawer> {
  final TextEditingController _searchCtrl = TextEditingController();
  String _query = '';

  @override
  void dispose() {
    _searchCtrl.dispose();
    super.dispose();
  }

  void _onSearchChanged(String v) {
    setState(() => _query = v.trim());
  }

  @override
  Widget build(BuildContext context) {
    final detailAsync = ref.watch(docDetailProvider(widget.specId));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _Header(specId: widget.specId, detailAsync: detailAsync),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
          child: TextField(
            key: const Key('toc_search_input'),
            controller: _searchCtrl,
            onChanged: _onSearchChanged,
            decoration: InputDecoration(
              isDense: true,
              hintText: '搜索本 spec 内容…',
              prefixIcon: const Icon(Icons.search, size: 18),
              suffixIcon: _query.isEmpty
                  ? null
                  : IconButton(
                      key: const Key('toc_search_clear'),
                      icon: const Icon(Icons.clear, size: 16),
                      onPressed: () {
                        _searchCtrl.clear();
                        _onSearchChanged('');
                      },
                    ),
            ),
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: _query.isEmpty
              ? _TocList(
                  detailAsync: detailAsync,
                  currentSectionPath: widget.currentSectionPath,
                  onSelect: widget.onSelectSection,
                )
              : _SearchResults(
                  specId: widget.specId,
                  query: _query,
                  onSelect: widget.onSelectChunk,
                ),
        ),
      ],
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.specId, required this.detailAsync});

  final String specId;
  final AsyncValue<DocDetailResponse> detailAsync;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            specId,
            key: const Key('toc_spec_id'),
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 2),
          detailAsync.maybeWhen(
            data: (d) => Text(
              '${d.release} · series ${d.series}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            orElse: () => Text(
              '加载中…',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          ),
        ],
      ),
    );
  }
}

class _TocList extends StatelessWidget {
  const _TocList({
    required this.detailAsync,
    required this.currentSectionPath,
    required this.onSelect,
  });

  final AsyncValue<DocDetailResponse> detailAsync;
  final String? currentSectionPath;
  final void Function(SectionNode node) onSelect;

  @override
  Widget build(BuildContext context) {
    return detailAsync.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Padding(
        padding: const EdgeInsets.all(16),
        child: Text(
          '加载章节树失败：$e',
          key: const Key('toc_error'),
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ),
      data: (d) {
        if (d.sections.isEmpty) {
          return Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              '该 spec 没有可显示的章节。',
              style: Theme.of(context).textTheme.bodySmall,
            ),
          );
        }
        return ListView.builder(
          key: const Key('toc_list'),
          itemCount: d.sections.length,
          itemBuilder: (ctx, i) {
            final s = d.sections[i];
            final isCurrent = s.joinedPath == currentSectionPath;
            return _TocTile(
              node: s,
              selected: isCurrent,
              onTap: () => onSelect(s),
            );
          },
        );
      },
    );
  }
}

class _TocTile extends StatelessWidget {
  const _TocTile({
    required this.node,
    required this.selected,
    required this.onTap,
  });

  final SectionNode node;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final depth = node.sectionPath.length;
    return Material(
      color: selected ? scheme.surfaceContainerHighest : Colors.transparent,
      child: InkWell(
        key: Key('toc_tile_${node.joinedPath}'),
        onTap: onTap,
        child: Padding(
          padding: EdgeInsets.only(
            left: 12 + (depth - 1).clamp(0, 6) * 12.0,
            right: 12,
            top: 6,
            bottom: 6,
          ),
          child: Row(
            children: [
              SizedBox(
                width: 56,
                child: Text(
                  node.joinedPath,
                  style: TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 12,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
              ),
              const SizedBox(width: 4),
              Expanded(
                child: Text(
                  node.sectionTitle.isEmpty ? '(untitled)' : node.sectionTitle,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 13,
                    fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
                    color: selected ? scheme.primary : scheme.onSurface,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _SearchResults extends ConsumerWidget {
  const _SearchResults({
    required this.specId,
    required this.query,
    required this.onSelect,
  });

  final String specId;
  final String query;
  final void Function(SearchHit hit) onSelect;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(docSearchProvider(SearchRef(specId: specId, query: query)));
    return async.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (e, _) => Padding(
        padding: const EdgeInsets.all(16),
        child: Text(
          '搜索失败：$e',
          key: const Key('toc_search_error'),
          style: Theme.of(context).textTheme.bodySmall,
        ),
      ),
      data: (resp) {
        if (resp.items.isEmpty) {
          return Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              '没找到匹配。',
              key: const Key('toc_search_empty'),
              style: Theme.of(context).textTheme.bodySmall,
            ),
          );
        }
        return ListView.builder(
          key: const Key('toc_search_list'),
          itemCount: resp.items.length,
          itemBuilder: (ctx, i) {
            final hit = resp.items[i];
            return ListTile(
              key: Key('toc_search_hit_${hit.chunkId}'),
              dense: true,
              title: Text(
                '${hit.specId} §${hit.joinedPath}',
                style: const TextStyle(fontSize: 12, fontFamily: 'monospace'),
              ),
              subtitle: Text(
                hit.sectionTitle.isEmpty ? hit.preview : hit.sectionTitle,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12),
              ),
              onTap: () => onSelect(hit),
            );
          },
        );
      },
    );
  }
}
