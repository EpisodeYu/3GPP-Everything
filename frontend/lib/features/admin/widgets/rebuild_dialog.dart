import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/api/admin_api.dart';

/// 重建索引弹框：
///
/// - spec_id 留空 → 全量重建
/// - force=true → 在已有索引上强制重跑（CLI 端按 purge_first 处理）
///
/// 调用后端 `POST /admin/index/rebuild` 返回 queued TaskOut，弹回 [true] 让外层
/// snackbar 提示用户去任务页查看。
class RebuildIndexDialog extends ConsumerStatefulWidget {
  const RebuildIndexDialog({super.key});

  @override
  ConsumerState<RebuildIndexDialog> createState() =>
      _RebuildIndexDialogState();
}

class _RebuildIndexDialogState extends ConsumerState<RebuildIndexDialog> {
  final TextEditingController _spec = TextEditingController();
  bool _force = false;
  bool _submitting = false;
  String? _error;

  @override
  void dispose() {
    _spec.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() {
      _submitting = true;
      _error = null;
    });
    final raw = _spec.text.trim();
    try {
      await ref.read(adminApiProvider).triggerIndexRebuild(
            specId: raw.isEmpty ? null : raw,
            force: _force,
          );
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } on Object catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _submitting = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      key: const Key('admin_rebuild_dialog'),
      title: const Text('重建索引'),
      content: SizedBox(
        width: 380,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              key: const Key('admin_rebuild_spec_input'),
              controller: _spec,
              decoration: const InputDecoration(
                labelText: 'spec_id（留空 = 全量重建）',
                hintText: '如 23.501',
              ),
              autofocus: true,
            ),
            const SizedBox(height: 12),
            SwitchListTile(
              key: const Key('admin_rebuild_force_switch'),
              contentPadding: EdgeInsets.zero,
              title: const Text('force（在已有索引上 purge 重跑）'),
              value: _force,
              onChanged: _submitting
                  ? null
                  : (v) => setState(() => _force = v),
            ),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  _error!,
                  key: const Key('admin_rebuild_error'),
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                    fontSize: 12,
                  ),
                ),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          key: const Key('admin_rebuild_cancel'),
          onPressed:
              _submitting ? null : () => Navigator.of(context).pop(false),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const Key('admin_rebuild_confirm'),
          onPressed: _submitting ? null : _submit,
          child: Text(_submitting ? '提交中…' : '提交'),
        ),
      ],
    );
  }
}
