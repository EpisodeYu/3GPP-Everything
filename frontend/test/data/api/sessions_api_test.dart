import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/sessions_api.dart';

class _Recorded {
  _Recorded(this.method, this.path, this.queryParameters, this.data);
  final String method;
  final String path;
  final Map<String, dynamic> queryParameters;
  final Object? data;
}

/// 极简 scripted adapter：按调用顺序消费 [scripts]，把每次请求记到 [calls]。
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
    final fn = scripts[_i++];
    return fn(options);
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

ResponseBody _empty(int status) => ResponseBody.fromString(
      '',
      status,
      headers: {
        'content-type': ['application/json'],
      },
    );

Map<String, dynamic> _sessionJson({
  required String id,
  String title = '',
  String status = 'active',
  String? forkedFrom,
}) =>
    {
      'id': id,
      'user_id': '00000000-0000-0000-0000-000000000001',
      'title': title,
      'mode_default': 'qa',
      'status': status,
      'forked_from_session_id': forkedFrom,
      'forked_from_checkpoint_id': null,
      'last_message_at': null,
      'created_at': '2026-05-24T08:00:00Z',
      'updated_at': '2026-05-24T08:30:00Z',
    };

Dio _makeDio(_ScriptedAdapter adapter) {
  return Dio(BaseOptions(baseUrl: 'http://test/api/v1'))
    ..httpClientAdapter = adapter;
}

void main() {
  group('SessionsApi (CRUD over scripted dio adapter)', () {
    test('list 解析 items + total，发送分页 query', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'items': [
                _sessionJson(id: 'sid-1', title: '调研 PDU Session'),
                _sessionJson(id: 'sid-2', status: 'archived_branch'),
              ],
              'total': 2,
            }),
      ]);
      final api = SessionsApi(_makeDio(adapter));

      final resp = await api.list(page: 1, pageSize: 50);

      expect(resp.total, 2);
      expect(resp.items.length, 2);
      expect(resp.items[0].id, 'sid-1');
      expect(resp.items[0].displayTitle, '调研 PDU Session');
      expect(resp.items[1].isArchivedBranch, true);
      expect(adapter.calls.single.method, 'GET');
      expect(adapter.calls.single.path, '/sessions');
      expect(adapter.calls.single.queryParameters,
          {'page': 1, 'page_size': 50});
    });

    test('create 发送 title + mode_default，解析返回 SessionOut', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(
              201,
              _sessionJson(id: 'sid-new', title: 'hello'),
            ),
      ]);
      final api = SessionsApi(_makeDio(adapter));

      final created = await api.create(title: 'hello');

      expect(created.id, 'sid-new');
      expect(created.title, 'hello');
      final call = adapter.calls.single;
      expect(call.method, 'POST');
      expect(call.path, '/sessions');
      expect(call.data, {'title': 'hello', 'mode_default': 'qa'});
    });

    test('get 走 /sessions/{sid}', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, _sessionJson(id: 'sid-3', title: '历史会话')),
      ]);
      final api = SessionsApi(_makeDio(adapter));

      final s = await api.get('sid-3');

      expect(s.id, 'sid-3');
      expect(s.title, '历史会话');
      expect(adapter.calls.single.method, 'GET');
      expect(adapter.calls.single.path, '/sessions/sid-3');
    });

    test('patch 只传非 null 字段，PATCH /sessions/{sid}', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(
              200,
              _sessionJson(id: 'sid-4', title: '新标题'),
            ),
      ]);
      final api = SessionsApi(_makeDio(adapter));

      final updated = await api.patch('sid-4', title: '新标题');

      expect(updated.title, '新标题');
      final call = adapter.calls.single;
      expect(call.method, 'PATCH');
      expect(call.path, '/sessions/sid-4');
      expect(call.data, {'title': '新标题'});
    });

    test('delete 走 DELETE /sessions/{sid}，204 不抛', () async {
      final adapter = _ScriptedAdapter([
        (_) => _empty(204),
      ]);
      final api = SessionsApi(_makeDio(adapter));

      await api.delete('sid-5');

      expect(adapter.calls.single.method, 'DELETE');
      expect(adapter.calls.single.path, '/sessions/sid-5');
    });

    test('list 4xx 错误冒泡为 DioException', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(401, {'code': 'unauthorized', 'message': 'no'}),
      ]);
      final api = SessionsApi(_makeDio(adapter));

      await expectLater(api.list(), throwsA(isA<DioException>()));
    });
  });
}
