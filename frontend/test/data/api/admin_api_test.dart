import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/admin_api.dart';

class _Recorded {
  _Recorded(this.method, this.path, this.queryParameters, this.data);
  final String method;
  final String path;
  final Map<String, dynamic> queryParameters;
  final Object? data;
}

class _ScriptedAdapter implements HttpClientAdapter {
  _ScriptedAdapter(this.scripts);
  final List<ResponseBody Function(RequestOptions)> scripts;
  final List<_Recorded> calls = [];
  int _i = 0;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<dynamic>? cancelFuture,
  ) async {
    calls.add(_Recorded(
      options.method,
      options.path,
      Map<String, dynamic>.from(options.queryParameters),
      options.data,
    ));
    return scripts[_i++](options);
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(int status, Map<String, dynamic> body) =>
    ResponseBody.fromString(
      jsonEncode(body),
      status,
      headers: {
        'content-type': ['application/json'],
      },
    );

Dio _makeDio(_ScriptedAdapter adapter) =>
    Dio(BaseOptions(baseUrl: 'http://test/api/v1'))
      ..httpClientAdapter = adapter;

Map<String, dynamic> _statsJson() => {
      'documents': 1270,
      'chunks': 394859,
      'users': 3,
      'sessions': 12,
      'messages': 88,
      'tasks': {'queued': 1, 'running': 1, 'done': 7, 'failed': 0},
      'api_usage_7d': {
        'llm_input_tokens': 12345,
        'llm_output_tokens': 6789,
        'embedding_tokens': 222,
        'rerank_calls': 11,
        'web_search_calls': 3,
        'total_cost_usd': 0.1234,
      },
    };

Map<String, dynamic> _taskJson(String id, {String status = 'queued'}) => {
      'id': id,
      'kind': 'index_rebuild',
      'payload': {'spec_id': '23.501', 'force': false},
      'status': status,
      'progress': 0,
      'log_tail': '',
      'started_at': null,
      'finished_at': null,
      'created_by': '00000000-0000-0000-0000-000000000001',
      'created_at': '2026-05-25T10:00:00Z',
    };

void main() {
  group('AdminApi', () {
    test('getStats 解析所有字段', () async {
      final adapter = _ScriptedAdapter([(_) => _json(200, _statsJson())]);
      final api = AdminApi(_makeDio(adapter));
      final s = await api.getStats();
      expect(s.documents, 1270);
      expect(s.chunks, 394859);
      expect(s.tasks['done'], 7);
      expect(s.apiUsage7d.llmInputTokens, 12345);
      expect(s.apiUsage7d.totalCostUsd, closeTo(0.1234, 1e-9));
      final call = adapter.calls.single;
      expect(call.method, 'GET');
      expect(call.path, '/admin/stats');
    });

    test('listTasks 默认带 page/page_size，无 status 时不带 status query', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'items': [_taskJson('t-1'), _taskJson('t-2', status: 'running')],
              'total': 2,
            }),
      ]);
      final api = AdminApi(_makeDio(adapter));
      final r = await api.listTasks();
      expect(r.total, 2);
      expect(r.items.first.id, 't-1');
      expect(r.items.last.status, 'running');
      final call = adapter.calls.single;
      expect(call.path, '/admin/tasks');
      expect(call.queryParameters['page'], 1);
      expect(call.queryParameters['page_size'], 50);
      expect(call.queryParameters.containsKey('status'), isFalse);
    });

    test('listTasks 带 statusFilter 走 query', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {'items': const [], 'total': 0}),
      ]);
      final api = AdminApi(_makeDio(adapter));
      await api.listTasks(statusFilter: 'running', page: 2, pageSize: 10);
      final call = adapter.calls.single;
      expect(call.queryParameters['status'], 'running');
      expect(call.queryParameters['page'], 2);
      expect(call.queryParameters['page_size'], 10);
    });

    test('getTask 拉单任务详情', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, _taskJson('t-9', status: 'done')),
      ]);
      final api = AdminApi(_makeDio(adapter));
      final t = await api.getTask('t-9');
      expect(t.id, 't-9');
      expect(t.status, 'done');
      expect(adapter.calls.single.path, '/admin/tasks/t-9');
    });

    test('triggerIndexRebuild 透传 spec_id + force', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(202, _taskJson('t-new', status: 'queued')),
      ]);
      final api = AdminApi(_makeDio(adapter));
      final t = await api.triggerIndexRebuild(specId: '23.501', force: true);
      expect(t.id, 't-new');
      expect(t.status, 'queued');
      final call = adapter.calls.single;
      expect(call.method, 'POST');
      expect(call.path, '/admin/index/rebuild');
      final body = call.data as Map<String, dynamic>;
      expect(body['spec_id'], '23.501');
      expect(body['force'], isTrue);
    });

    test('triggerIndexRebuild 留空 spec_id → 全量重建', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(202, _taskJson('t-full', status: 'queued')),
      ]);
      final api = AdminApi(_makeDio(adapter));
      await api.triggerIndexRebuild();
      final body = adapter.calls.single.data as Map<String, dynamic>;
      expect(body['spec_id'], isNull);
      expect(body['force'], isFalse);
    });

    test('403 冒泡 DioException（非 admin 调用兜底）', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(403, {'code': 'forbidden'}),
      ]);
      final api = AdminApi(_makeDio(adapter));
      await expectLater(api.getStats(), throwsA(isA<DioException>()));
    });
  });
}
