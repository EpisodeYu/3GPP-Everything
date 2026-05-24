import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/interceptors.dart';

import '../../support/fake_token_store.dart';

class _ScriptedAdapter implements HttpClientAdapter {
  _ScriptedAdapter(this.script);

  final List<ResponseBody Function(RequestOptions)> script;
  // dio 在 retry 时会复用同一个 RequestOptions 并就地改 headers，
  // 因此必须在 fetch 当下把 headers / extra 拍快照，不然测试看到的全是最后一次的值。
  final List<Map<String, dynamic>> headers = [];
  final List<dynamic> retriedExtra = [];
  int callCount = 0;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<dynamic>? cancelFuture,
  ) async {
    final index = callCount;
    headers.add(Map<String, dynamic>.from(options.headers));
    retriedExtra.add(options.extra['retried']);
    callCount += 1;
    return script[index](options);
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

void main() {
  group('AuthInterceptor', () {
    test('注入 Authorization 头并直通 2xx', () async {
      final store = FakeTokenStore(access: 'A1', refresh: 'R1');
      final adapter = _ScriptedAdapter([
        (req) => _json(200, {'ok': true}),
      ]);
      final dio = Dio()..httpClientAdapter = adapter;
      dio.interceptors.add(AuthInterceptor(
        tokenStore: store,
        onRefresh: () async => null,
        onAuthLost: () {},
        retry: dio.fetch,
      ));

      final resp = await dio.get<Map<String, dynamic>>('http://x/me');
      expect(resp.statusCode, 200);
      expect(adapter.callCount, 1);
      expect(adapter.headers.single['Authorization'], 'Bearer A1');
    });

    test('401 → refresh 成功 → 用新 access 重放一次', () async {
      final store = FakeTokenStore(access: 'A1', refresh: 'R1');
      final adapter = _ScriptedAdapter([
        (req) => _json(401, {'code': 'unauthorized'}),
        (req) => _json(200, {'ok': true}),
      ]);
      final dio = Dio()..httpClientAdapter = adapter;
      var refreshCalls = 0;
      var lostCalls = 0;
      dio.interceptors.add(AuthInterceptor(
        tokenStore: store,
        onRefresh: () async {
          refreshCalls += 1;
          await store.write(access: 'A2', refresh: 'R2');
          return 'A2';
        },
        onAuthLost: () => lostCalls += 1,
        retry: dio.fetch,
      ));

      final resp = await dio.get<Map<String, dynamic>>('http://x/me');
      expect(resp.statusCode, 200);
      expect(refreshCalls, 1);
      expect(lostCalls, 0);
      expect(adapter.callCount, 2);
      expect(adapter.headers[0]['Authorization'], 'Bearer A1');
      expect(adapter.headers[1]['Authorization'], 'Bearer A2');
      expect(adapter.retriedExtra[1], true);
    });

    test('401 → refresh 返回 null → onAuthLost 被调一次，不再重放', () async {
      final store = FakeTokenStore(access: 'A1', refresh: 'R1');
      final adapter = _ScriptedAdapter([
        (req) => _json(401, {'code': 'unauthorized'}),
      ]);
      final dio = Dio()..httpClientAdapter = adapter;
      var lostCalls = 0;
      dio.interceptors.add(AuthInterceptor(
        tokenStore: store,
        onRefresh: () async => null,
        onAuthLost: () => lostCalls += 1,
        retry: dio.fetch,
      ));

      await expectLater(
        dio.get<Map<String, dynamic>>('http://x/me'),
        throwsA(isA<DioException>()),
      );
      expect(lostCalls, 1);
      expect(adapter.callCount, 1);
    });

    test('重放后仍 401 → 不再进入 refresh 循环', () async {
      final store = FakeTokenStore(access: 'A1', refresh: 'R1');
      final adapter = _ScriptedAdapter([
        (req) => _json(401, {'code': 'unauthorized'}),
        (req) => _json(401, {'code': 'unauthorized'}),
      ]);
      final dio = Dio()..httpClientAdapter = adapter;
      var refreshCalls = 0;
      dio.interceptors.add(AuthInterceptor(
        tokenStore: store,
        onRefresh: () async {
          refreshCalls += 1;
          return 'A2';
        },
        onAuthLost: () {},
        retry: dio.fetch,
      ));

      await expectLater(
        dio.get<Map<String, dynamic>>('http://x/me'),
        throwsA(isA<DioException>()),
      );
      expect(refreshCalls, 1);
      expect(adapter.callCount, 2);
    });
  });
}
