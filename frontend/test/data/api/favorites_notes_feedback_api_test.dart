import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/favorites_api.dart';
import 'package:tgpp/data/api/feedback_api.dart';
import 'package:tgpp/data/api/notes_api.dart';

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
    calls.add(_Recorded(options.method, options.path,
        Map<String, dynamic>.from(options.queryParameters), options.data));
    return scripts[_i++](options);
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(int status, Map<String, dynamic> body) =>
    ResponseBody.fromString(jsonEncode(body), status, headers: {
      'content-type': ['application/json'],
    });

ResponseBody _empty(int status) =>
    ResponseBody.fromString('', status, headers: {
      'content-type': ['application/json'],
    });

Dio _makeDio(_ScriptedAdapter adapter) =>
    Dio(BaseOptions(baseUrl: 'http://test/api/v1'))..httpClientAdapter = adapter;

void main() {
  group('FavoritesApi', () {
    test('create POST /favorites', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(201, {
              'id': 'fav-1',
              'target_type': 'message',
              'target_id': 'msg-9',
              'created_at': '2026-05-24T21:00:00Z',
            }),
      ]);
      final api = FavoritesApi(_makeDio(adapter));

      final f = await api.create(targetType: 'message', targetId: 'msg-9');

      expect(f.id, 'fav-1');
      expect(f.targetType, 'message');
      expect(f.targetId, 'msg-9');
      final c = adapter.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/favorites');
      expect(c.data, {'target_type': 'message', 'target_id': 'msg-9'});
    });

    test('list 带 target_type query', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {'items': []}),
      ]);
      final api = FavoritesApi(_makeDio(adapter));

      final resp = await api.list(targetType: 'chunk');

      expect(resp.items, isEmpty);
      expect(adapter.calls.single.queryParameters, {'target_type': 'chunk'});
    });

    test('delete DELETE /favorites/{fid}', () async {
      final adapter = _ScriptedAdapter([(_) => _empty(204)]);
      final api = FavoritesApi(_makeDio(adapter));

      await api.delete('fav-1');

      expect(adapter.calls.single.method, 'DELETE');
      expect(adapter.calls.single.path, '/favorites/fav-1');
    });
  });

  group('NotesApi', () {
    test('create POST /notes', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(201, {
              'id': 'n-1',
              'target_type': 'message',
              'target_id': 'msg-1',
              'body': 'PDU 流程',
              'created_at': '2026-05-24T21:00:00Z',
              'updated_at': '2026-05-24T21:00:00Z',
            }),
      ]);
      final api = NotesApi(_makeDio(adapter));

      final n = await api.create(
        targetType: 'message',
        targetId: 'msg-1',
        body: 'PDU 流程',
      );

      expect(n.id, 'n-1');
      expect(n.body, 'PDU 流程');
      expect(adapter.calls.single.data, {
        'target_type': 'message',
        'target_id': 'msg-1',
        'body': 'PDU 流程',
      });
    });

    test('patch PATCH /notes/{nid}', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'id': 'n-1',
              'target_type': 'message',
              'target_id': 'msg-1',
              'body': '改了',
              'created_at': '2026-05-24T21:00:00Z',
              'updated_at': '2026-05-24T22:00:00Z',
            }),
      ]);
      final api = NotesApi(_makeDio(adapter));

      final n = await api.patch('n-1', body: '改了');

      expect(n.body, '改了');
      expect(adapter.calls.single.method, 'PATCH');
      expect(adapter.calls.single.path, '/notes/n-1');
      expect(adapter.calls.single.data, {'body': '改了'});
    });
  });

  group('FeedbackApi', () {
    test('upsert POST /messages/{mid}/feedback', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(201, {
              'id': 'fb-1',
              'message_id': 'msg-1',
              'thumb': 1,
              'reason': null,
              'created_at': '2026-05-24T21:00:00Z',
            }),
      ]);
      final api = FeedbackApi(_makeDio(adapter));

      final f = await api.upsert('msg-1', thumb: 1);

      expect(f.thumb, 1);
      expect(f.reason, isNull);
      final c = adapter.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/messages/msg-1/feedback');
      expect(c.data, {'thumb': 1});
    });

    test('upsert 带 reason 时附加到 body', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(201, {
              'id': 'fb-2',
              'message_id': 'msg-2',
              'thumb': -1,
              'reason': '答错了',
              'created_at': '2026-05-24T21:00:00Z',
            }),
      ]);
      final api = FeedbackApi(_makeDio(adapter));

      final f = await api.upsert('msg-2', thumb: -1, reason: '答错了');

      expect(f.reason, '答错了');
      expect(adapter.calls.single.data, {'thumb': -1, 'reason': '答错了'});
    });

    test('upsert 4xx → DioException', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(404, {'code': 'message_not_found'}),
      ]);
      final api = FeedbackApi(_makeDio(adapter));

      await expectLater(
        api.upsert('missing', thumb: 1),
        throwsA(isA<DioException>()),
      );
    });
  });
}
