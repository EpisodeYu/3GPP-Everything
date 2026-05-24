import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/checkpoint_api.dart';
import 'package:tgpp/data/api/messages_api.dart';

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

/// 构造一段 SSE 流字节流（resume 路由用 stream response，
/// 与 messages_api.sendMessage 同款）。
ResponseBody _sseStream(String body) {
  final controller = StreamController<Uint8List>();
  controller.add(Uint8List.fromList(utf8.encode(body)));
  controller.close();
  return ResponseBody(
    controller.stream,
    200,
    headers: {
      'content-type': ['text/event-stream'],
    },
  );
}

Dio _makeDio(_ScriptedAdapter adapter) {
  return Dio(BaseOptions(baseUrl: 'http://test/api/v1'))
    ..httpClientAdapter = adapter;
}

Map<String, dynamic> _sessionJson(String id) => {
      'id': id,
      'user_id': '00000000-0000-0000-0000-000000000001',
      'title': 'forked',
      'mode_default': 'qa',
      'status': 'active',
      'forked_from_session_id': 'src-sid',
      'forked_from_checkpoint_id': 'cp-1',
      'last_message_at': null,
      'created_at': '2026-05-24T20:00:00Z',
      'updated_at': '2026-05-24T20:00:00Z',
    };

void main() {
  group('CheckpointApi (5 routes over scripted dio adapter)', () {
    test('pause POST /sessions/{sid}/runs/{rid}/pause', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'run_id': 'r-1',
              'session_id': 'sid-1',
              'status': 'paused',
            }),
      ]);
      final api = CheckpointApi(_makeDio(adapter));
      final resp = await api.pause('sid-1', 'r-1');

      expect(resp.runId, 'r-1');
      expect(resp.sessionId, 'sid-1');
      expect(resp.status, 'paused');
      final c = adapter.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/sessions/sid-1/runs/r-1/pause');
    });

    test('resume POST /sessions/{sid}/resume 返回 SSE 事件流', () async {
      final sse = StringBuffer()
        ..writeln('event: run_start')
        ..writeln('data: {"run_id":"r","session_id":"sid","message_id":"m"}')
        ..writeln('')
        ..writeln('event: token')
        ..writeln('data: {"delta":"hi"}')
        ..writeln('')
        ..writeln('event: final')
        ..writeln(
            'data: {"message_id":"m","answer":"hi","citations":[],"confidence":0.8}')
        ..writeln('')
        ..writeln('event: end')
        ..writeln('data: {}')
        ..writeln('');
      final adapter = _ScriptedAdapter([
        (_) => _sseStream(sse.toString()),
      ]);
      final api = CheckpointApi(_makeDio(adapter));

      final events = <ChatEvent>[];
      await for (final e in api.resume('sid-r')) {
        events.add(e);
      }

      expect(events, hasLength(4));
      expect(events[0], isA<RunStartEvent>());
      expect((events[1] as TokenEvent).delta, 'hi');
      expect((events[2] as FinalEvent).answer, 'hi');
      expect(events[3], isA<EndEvent>());
      expect(adapter.calls.single.method, 'POST');
      expect(adapter.calls.single.path, '/sessions/sid-r/resume');
    });

    test('list GET /sessions/{sid}/checkpoints', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'items': [
                {
                  'checkpoint_id': 'cp-2',
                  'parent_checkpoint_id': 'cp-1',
                  'created_at': '2026-05-24T20:01:00Z',
                  'next_nodes': ['generate'],
                  'last_node': 'rerank',
                },
                {
                  'checkpoint_id': 'cp-1',
                  'parent_checkpoint_id': null,
                  'created_at': '2026-05-24T20:00:00Z',
                  'next_nodes': [],
                  'last_node': null,
                },
              ],
            }),
      ]);
      final api = CheckpointApi(_makeDio(adapter));
      final resp = await api.list('sid-l');

      expect(resp.items.length, 2);
      expect(resp.items[0].checkpointId, 'cp-2');
      expect(resp.items[0].parentCheckpointId, 'cp-1');
      expect(resp.items[0].lastNode, 'rerank');
      expect(resp.items[0].nextNodes, ['generate']);
      expect(resp.items[1].parentCheckpointId, isNull);
      expect(adapter.calls.single.method, 'GET');
      expect(adapter.calls.single.path, '/sessions/sid-l/checkpoints');
    });

    test('fork POST /sessions/{sid}/fork 返回新 SessionOut', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(201, {'new_session': _sessionJson('new-sid')}),
      ]);
      final api = CheckpointApi(_makeDio(adapter));

      final resp = await api.fork(
        'src-sid',
        checkpointId: 'cp-1',
        newUserMessage: '换个问法',
        title: '分叉测试',
      );

      expect(resp.newSession.id, 'new-sid');
      expect(resp.newSession.forkedFromSessionId, 'src-sid');
      final c = adapter.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/sessions/src-sid/fork');
      expect(c.data, {
        'checkpoint_id': 'cp-1',
        'new_user_message': '换个问法',
        'title': '分叉测试',
      });
    });

    test('fork 不传 newUserMessage / title 时 body 只含 checkpoint_id', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(201, {'new_session': _sessionJson('new-sid')}),
      ]);
      final api = CheckpointApi(_makeDio(adapter));

      await api.fork('src-sid', checkpointId: 'cp-1');

      expect(adapter.calls.single.data, {'checkpoint_id': 'cp-1'});
    });

    test('rollback POST /sessions/{sid}/rollback', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'deleted_messages': 4,
              'head_checkpoint_id': 'cp-head',
            }),
      ]);
      final api = CheckpointApi(_makeDio(adapter));

      final resp = await api.rollback('sid-rb', lastN: 2);

      expect(resp.deletedMessages, 4);
      expect(resp.headCheckpointId, 'cp-head');
      final c = adapter.calls.single;
      expect(c.method, 'POST');
      expect(c.path, '/sessions/sid-rb/rollback');
      expect(c.data, {'last_n': 2});
    });

    test('pause 4xx → DioException', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(409, {'code': 'session_archived', 'message': 'no'}),
      ]);
      final api = CheckpointApi(_makeDio(adapter));

      await expectLater(
        api.pause('sid', 'rid'),
        throwsA(isA<DioException>()),
      );
    });
  });
}
