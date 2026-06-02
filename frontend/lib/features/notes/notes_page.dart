import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../data/api/notes_api.dart';
import '../library/library_widgets.dart';

/// 「我的笔记」独立页（侧栏入口 push 进入）。
///
/// 列出当前用户的笔记；message 类型条目后端已 enrich 出 session_id + 内容预览，
/// 点击跳回原消息，右侧可编辑（PATCH）/ 删除。
final notesListProvider = FutureProvider.autoDispose<NoteListResponse>(
  (ref) => ref.watch(notesApiProvider).list(),
);

class NotesPage extends ConsumerWidget {
  const NotesPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final async = ref.watch(notesListProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('我的笔记')),
      body: async.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (e, _) => LibraryError(
          message: '加载笔记失败：$e',
          onRetry: () => ref.invalidate(notesListProvider),
        ),
        data: (resp) {
          if (resp.items.isEmpty) {
            return const LibraryEmpty(
              icon: Icons.sticky_note_2_outlined,
              text: '还没有笔记。在回答上长按 → 添加笔记。',
            );
          }
          return RefreshIndicator(
            onRefresh: () async => ref.invalidate(notesListProvider),
            child: ListView.separated(
              key: const Key('notes_list'),
              padding: const EdgeInsets.symmetric(vertical: 8),
              itemCount: resp.items.length,
              separatorBuilder: (_, _) => const Divider(height: 1),
              itemBuilder: (context, i) {
                final n = resp.items[i];
                return _NoteTile(
                  note: n,
                  onTap: n.sessionId == null
                      ? null
                      : () => context.go(
                            '/sessions/${n.sessionId}?msg=${n.targetId}',
                          ),
                  onEdit: () => _edit(context, ref, n),
                  onDelete: () => _delete(context, ref, n.id),
                );
              },
            ),
          );
        },
      ),
    );
  }

  Future<void> _edit(BuildContext context, WidgetRef ref, NoteOut n) async {
    final newBody = await showDialog<String>(
      context: context,
      builder: (_) => _NoteEditDialog(initial: n.body),
    );
    if (newBody == null) return;
    try {
      await ref.read(notesApiProvider).patch(n.id, body: newBody.trim());
      ref.invalidate(notesListProvider);
    } on Object catch (e) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('保存失败：$e')),
      );
    }
  }

  Future<void> _delete(BuildContext context, WidgetRef ref, String nid) async {
    try {
      await ref.read(notesApiProvider).delete(nid);
      ref.invalidate(notesListProvider);
    } on Object catch (e) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('删除失败：$e')),
      );
    }
  }
}

class _NoteTile extends StatelessWidget {
  const _NoteTile({
    required this.note,
    this.onTap,
    required this.onEdit,
    required this.onDelete,
  });
  final NoteOut note;
  final VoidCallback? onTap;
  final VoidCallback onEdit;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final src = note.preview ??
        (note.targetType == 'message' ? '（原消息已删除）' : note.targetId);
    return ListTile(
      key: Key('note_tile_${note.id}'),
      leading: const Icon(Icons.sticky_note_2_outlined),
      title: Text(
        note.body.isEmpty ? '（空笔记）' : note.body,
        maxLines: 3,
        overflow: TextOverflow.ellipsis,
      ),
      subtitle: Text(
        '原文：$src\n${formatLibraryTime(note.updatedAt)}',
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
      ),
      isThreeLine: true,
      onTap: onTap,
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          IconButton(
            key: Key('note_edit_${note.id}'),
            icon: const Icon(Icons.edit_outlined),
            tooltip: '编辑',
            onPressed: onEdit,
          ),
          IconButton(
            key: Key('note_delete_${note.id}'),
            icon: const Icon(Icons.delete_outline),
            tooltip: '删除',
            onPressed: onDelete,
          ),
        ],
      ),
    );
  }
}

class _NoteEditDialog extends StatefulWidget {
  const _NoteEditDialog({required this.initial});
  final String initial;

  @override
  State<_NoteEditDialog> createState() => _NoteEditDialogState();
}

class _NoteEditDialogState extends State<_NoteEditDialog> {
  late final TextEditingController _ctrl =
      TextEditingController(text: widget.initial);

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('编辑笔记'),
      content: TextField(
        key: const Key('note_edit_field'),
        controller: _ctrl,
        autofocus: true,
        minLines: 3,
        maxLines: 6,
        decoration: const InputDecoration(hintText: '写下你的笔记…'),
      ),
      actions: [
        TextButton(
          key: const Key('note_edit_cancel'),
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('note_edit_save'),
          onPressed: () => Navigator.pop(context, _ctrl.text),
          child: const Text('保存'),
        ),
      ],
    );
  }
}
