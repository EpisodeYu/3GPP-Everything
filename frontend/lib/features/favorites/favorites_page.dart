import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/api/favorites_api.dart';
import '../library/library_widgets.dart';

/// 「我的收藏」独立页（侧栏入口 push 进入）。
///
/// 列出当前用户的收藏；message 类型条目后端已 enrich 出 session_id + 内容预览，
/// 点击跳回原消息（`/sessions/{sid}?msg={mid}`），右侧可删除。
final favoritesListProvider = FutureProvider.autoDispose<FavoriteListResponse>(
  (ref) => ref.watch(favoritesApiProvider).list(),
);

class FavoritesPage extends ConsumerWidget {
  const FavoritesPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(favoritesListProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('我的收藏')),
      body: async.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => LibraryError(
          message: '加载收藏失败：$e',
          onRetry: () => ref.invalidate(favoritesListProvider),
        ),
        data: (resp) {
          if (resp.items.isEmpty) {
            return const LibraryEmpty(
              icon: Icons.bookmark_border,
              text: '还没有收藏。在回答上长按 → 收藏。',
            );
          }
          return RefreshIndicator(
            onRefresh: () async => ref.invalidate(favoritesListProvider),
            child: ListView.separated(
              key: const Key('favorites_list'),
              padding: const EdgeInsets.symmetric(vertical: 8),
              itemCount: resp.items.length,
              separatorBuilder: (_, _) => const Divider(height: 1),
              itemBuilder: (context, i) {
                final f = resp.items[i];
                return _FavoriteTile(
                  fav: f,
                  onTap: f.sessionId == null
                      ? null
                      : () => context.go(
                            '/sessions/${f.sessionId}?msg=${f.targetId}',
                          ),
                  onDelete: () => _delete(context, ref, f.id),
                );
              },
            ),
          );
        },
      ),
    );
  }

  Future<void> _delete(BuildContext context, WidgetRef ref, String fid) async {
    try {
      await ref.read(favoritesApiProvider).delete(fid);
      ref.invalidate(favoritesListProvider);
    } on Object catch (e) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('删除失败：$e')),
      );
    }
  }
}

class _FavoriteTile extends StatelessWidget {
  const _FavoriteTile({required this.fav, this.onTap, required this.onDelete});
  final FavoriteOut fav;
  final VoidCallback? onTap;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final preview = fav.preview ??
        (fav.targetType == 'message' ? '（原消息已删除）' : fav.targetId);
    return ListTile(
      key: Key('favorite_tile_${fav.id}'),
      leading: const Icon(Icons.bookmark),
      title: Text(preview, maxLines: 2, overflow: TextOverflow.ellipsis),
      subtitle: Text(formatLibraryTime(fav.createdAt)),
      onTap: onTap,
      trailing: IconButton(
        key: Key('favorite_delete_${fav.id}'),
        icon: const Icon(Icons.delete_outline),
        tooltip: '删除收藏',
        onPressed: onDelete,
      ),
    );
  }
}
