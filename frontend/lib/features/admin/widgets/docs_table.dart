import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/api/docs_api.dart';

/// 文档表：复用 `/docs` 路由（无需 admin 鉴权也可读，但 M5.5 把过滤 + 表格放在
/// 管理后台入口下，让管理员快速看 release/series 分布）。
class DocsTable extends ConsumerStatefulWidget {
  const DocsTable({super.key});

  @override
  ConsumerState<DocsTable> createState() => _DocsTableState();
}

class _DocsTableState extends ConsumerState<DocsTable> {
  String _release = '';
  String _series = '';

  @override
  Widget build(BuildContext context) {
    final async = ref.watch(_docsListProvider((_release, _series)));
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
          child: Row(
            children: [
              SizedBox(
                width: 160,
                child: TextField(
                  key: const Key('admin_docs_release'),
                  decoration: const InputDecoration(
                    isDense: true,
                    labelText: 'release（如 Rel-18）',
                  ),
                  onSubmitted: (v) => setState(() => _release = v.trim()),
                ),
              ),
              const SizedBox(width: 12),
              SizedBox(
                width: 140,
                child: TextField(
                  key: const Key('admin_docs_series'),
                  decoration: const InputDecoration(
                    isDense: true,
                    labelText: 'series（如 23）',
                  ),
                  onSubmitted: (v) => setState(() => _series = v.trim()),
                ),
              ),
              const SizedBox(width: 12),
              FilledButton.icon(
                key: const Key('admin_docs_apply'),
                onPressed: () => ref.invalidate(_docsListProvider),
                icon: const Icon(Icons.refresh),
                label: const Text('刷新'),
              ),
              const Spacer(),
              if (async case AsyncData(:final value))
                Text('共 ${value.total} 篇',
                    key: const Key('admin_docs_total'),
                    style: Theme.of(context).textTheme.bodySmall),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: async.when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => Center(
              key: const Key('admin_docs_error'),
              child: Text('加载文档列表失败：$e'),
            ),
            data: (resp) {
              if (resp.items.isEmpty) {
                return const Center(
                  key: Key('admin_docs_empty'),
                  child: Text('没有匹配的文档'),
                );
              }
              return Scrollbar(
                child: ListView.separated(
                  key: const Key('admin_docs_list'),
                  itemCount: resp.items.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (_, i) {
                    final d = resp.items[i];
                    return ListTile(
                      dense: true,
                      key: Key('admin_docs_row_${d.specId}'),
                      title: Text(d.specId),
                      subtitle: Text(
                        '${d.release} · series ${d.series}'
                        '${d.title.isNotEmpty ? ' · ${d.title}' : ''}',
                      ),
                      trailing: Text('${d.chunkCount} chunks'),
                    );
                  },
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

/// 按 (release, series) 缓存的文档列表。
final _docsListProvider =
    FutureProvider.family.autoDispose<DocListResponse, (String, String)>(
  (ref, key) async {
    final (release, series) = key;
    return ref.watch(docsApiProvider).list(
          release: release.isEmpty ? null : release,
          series: series.isEmpty ? null : series,
        );
  },
);
