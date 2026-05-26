import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/messages_api.dart';

/// MessagesApi.sendMessage 的 Options 钉死测：
///
/// 锚：2026-05-25 SSE receive-timeout fix。
/// 背景：dio 5.x BrowserHttpClientAdapter 把 `Options.receiveTimeout = Duration.zero`
/// 当作"未 override" → 退化到 BaseOptions 默认 30s → SSE 跑超过 30s 时直接报
/// `DioException [receive timeout]`。Hotfix：显式给 24h 大值绕过。
///
/// 本测保证未来不会有人改回 Duration.zero / 把它去掉。

class _Recorded {
  _Recorded({
    required this.method,
    required this.path,
    required this.headers,
    required this.responseType,
    required this.receiveTimeout,
    required this.sendTimeout,
  });
  final String method;
  final String path;
  final Map<String, dynamic> headers;
  final ResponseType? responseType;
  final Duration? receiveTimeout;
  final Duration? sendTimeout;
}

class _SseScriptedAdapter implements HttpClientAdapter {
  _SseScriptedAdapter(this.body);
  final String body;
  _Recorded? lastCall;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<dynamic>? cancelFuture,
  ) async {
    lastCall = _Recorded(
      method: options.method,
      path: options.path,
      headers: Map<String, dynamic>.from(options.headers),
      responseType: options.responseType,
      receiveTimeout: options.receiveTimeout,
      sendTimeout: options.sendTimeout,
    );
    final controller = StreamController<Uint8List>();
    controller.add(Uint8List.fromList(utf8.encode(body)));
    // sync close（不 await）：避免 close() 在没有 listener 时挂起。await for
    // 接管 stream 后会自然 drain 完 + 触发 done。
    unawaited(controller.close());
    return ResponseBody(
      controller.stream,
      200,
      headers: {
        'content-type': ['text/event-stream'],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

Dio _makeDio(_SseScriptedAdapter adapter) {
  // BaseOptions 故意设 receiveTimeout=30s，模拟生产 dio_provider 的 baseline，
  // 测 Options 必须能 override 它。
  return Dio(BaseOptions(
    baseUrl: 'http://test/api/v1',
    receiveTimeout: const Duration(seconds: 30),
  ))
    ..httpClientAdapter = adapter;
}

void main() {
  group('MessagesApi.sendMessage (SSE Options regression)', () {
    test(
      'POST stream + receive/sendTimeout 显式 24h（不能是 Duration.zero / null）',
      () async {
        const sseBody =
            'event: run_start\ndata: {"run_id":"r","session_id":"s","message_id":"m"}\n\n'
            'event: end\ndata: {}\n\n';
        final adapter = _SseScriptedAdapter(sseBody);
        final api = MessagesApi(_makeDio(adapter));

        final body = SendMessageBody(content: 'hi');
        final events = <ChatEvent>[];
        await for (final e in api.sendMessage('sid-1', body)) {
          events.add(e);
        }

        expect(events, hasLength(2));
        expect(events[0], isA<RunStartEvent>());
        expect(events[1], isA<EndEvent>());

        final c = adapter.lastCall!;
        expect(c.method, 'POST');
        expect(c.path, '/sessions/sid-1/messages');
        expect(c.responseType, ResponseType.stream,
            reason: 'SSE 必须 stream 模式');
        expect(c.headers['Accept'], 'text/event-stream');

        // 关键 regression assertion：dio 5.x web adapter 在 Duration.zero 上有
        // bug；必须显式给一个大值绕过。任何小于 1 小时的值都视为风险。
        expect(c.receiveTimeout, isNotNull,
            reason: 'sendMessage 必须显式 override receiveTimeout，'
                '不能让 BaseOptions 默认值生效');
        expect(c.receiveTimeout, isNot(Duration.zero),
            reason: 'Duration.zero 在 dio web 上不被识别为禁用 → 触发 receive timeout');
        expect(c.receiveTimeout!.inHours, greaterThanOrEqualTo(1),
            reason: 'SSE 完整 RAG 跑 30s–几分钟，timeout 必须 ≥ 1h');

        expect(c.sendTimeout, isNotNull,
            reason: '同 receiveTimeout，避免 web adapter 退化');
        expect(c.sendTimeout!.inHours, greaterThanOrEqualTo(1));
      },
    );

    test('解析 title 事件 → TitleEvent（首轮自动标题）', () async {
      const sseBody =
          'event: final\ndata: {"message_id":"m","answer":"a","citations":[],"confidence":0.5}\n\n'
          'event: title\ndata: {"session_id":"sid-1","title":"AMF 概述"}\n\n'
          'event: end\ndata: {}\n\n';
      final api = MessagesApi(_makeDio(_SseScriptedAdapter(sseBody)));

      final events = <ChatEvent>[];
      await for (final e in api.sendMessage('sid-1', SendMessageBody(content: 'hi'))) {
        events.add(e);
      }

      final title = events.whereType<TitleEvent>().single;
      expect(title.sessionId, 'sid-1');
      expect(title.title, 'AMF 概述');
      // title 在 final 之后、end 之前
      expect(events.indexWhere((e) => e is FinalEvent),
          lessThan(events.indexWhere((e) => e is TitleEvent)));
      expect(events.indexWhere((e) => e is TitleEvent),
          lessThan(events.indexWhere((e) => e is EndEvent)));
    });
  });
}
