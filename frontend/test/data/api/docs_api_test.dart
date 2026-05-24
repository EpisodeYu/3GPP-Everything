import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/docs_api.dart';

class _Recorded {
  _Recorded(this.method, this.path, this.queryParameters);
  final String method;
  final String path;
  final Map<String, dynamic> queryParameters;
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

void main() {
  group('DocsApi', () {
    test('list 解析 items + total，支持 release/series 过滤', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'items': [
                {
                  'spec_id': '23.501',
                  'release': 'Rel-18',
                  'series': '23',
                  'title': 'System Arch',
                  'chunk_count': 1234,
                },
              ],
              'total': 1,
            }),
      ]);
      final api = DocsApi(_makeDio(adapter));
      final resp = await api.list(release: 'Rel-18', series: '23');
      expect(resp.total, 1);
      expect(resp.items.first.specId, '23.501');
      expect(resp.items.first.chunkCount, 1234);
      final call = adapter.calls.single;
      expect(call.method, 'GET');
      expect(call.path, '/docs');
      expect(call.queryParameters, {'release': 'Rel-18', 'series': '23'});
    });

    test('list 默认不带 query', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {'items': [], 'total': 0}),
      ]);
      final api = DocsApi(_makeDio(adapter));
      await api.list();
      expect(adapter.calls.single.queryParameters, isEmpty);
    });

    test('getDoc 解析 sections', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'spec_id': '23.501',
              'release': 'Rel-18',
              'series': '23',
              'sections': [
                {
                  'section_path': ['5', '6', '1'],
                  'section_title': 'PDU Session',
                  'chunk_count': 3,
                },
                {
                  'section_path': ['5', '6', '1', '2'],
                  'section_title': 'PDU Establishment',
                  'chunk_count': 2,
                },
              ],
            }),
      ]);
      final api = DocsApi(_makeDio(adapter));
      final d = await api.getDoc('23.501');
      expect(d.specId, '23.501');
      expect(d.sections.length, 2);
      expect(d.sections.first.joinedPath, '5.6.1');
      expect(d.sections.last.joinedPath, '5.6.1.2');
      expect(adapter.calls.single.path, '/docs/23.501');
    });

    test('getSection 归一化 path（/ → .），返回 chunks', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'spec_id': '23.501',
              'section_path': ['5', '6', '1'],
              'section_title': 'PDU Session',
              'chunks': [
                {
                  'chunk_id': 'c-1',
                  'spec_id': '23.501',
                  'section_path': ['5', '6', '1'],
                  'section_title': 'PDU Session',
                  'chunk_type': 'text',
                  'content': 'hello world',
                },
              ],
            }),
      ]);
      final api = DocsApi(_makeDio(adapter));
      final s = await api.getSection('23.501', '5/6/1');
      expect(s.chunks.single.chunkId, 'c-1');
      expect(s.joinedPath, '5.6.1');
      expect(adapter.calls.single.path, '/docs/23.501/sections/5.6.1');
    });

    test('search 传 q 参数', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'spec_id': '23.501',
              'query': 'PDU',
              'items': [
                {
                  'chunk_id': 'c-9',
                  'spec_id': '23.501',
                  'section_path': ['5', '6'],
                  'section_title': '',
                  'chunk_type': 'text',
                  'preview': 'PDU Session...',
                },
              ],
            }),
      ]);
      final api = DocsApi(_makeDio(adapter));
      final r = await api.search('23.501', 'PDU');
      expect(r.items.single.chunkId, 'c-9');
      final call = adapter.calls.single;
      expect(call.path, '/docs/23.501/search');
      expect(call.queryParameters, {'q': 'PDU'});
    });

    test('getChunk 单 chunk', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(200, {
              'chunk_id': 'c-77',
              'spec_id': '23.501',
              'section_path': ['5', '6', '1'],
              'section_title': '',
              'chunk_type': 'text',
              'content': 'body',
              'raw_extra': {'a': 1},
            }),
      ]);
      final api = DocsApi(_makeDio(adapter));
      final c = await api.getChunk('c-77');
      expect(c.chunkId, 'c-77');
      expect(c.content, 'body');
      expect(c.rawExtra['a'], 1);
      expect(adapter.calls.single.path, '/chunks/c-77');
    });

    test('4xx 冒泡 DioException', () async {
      final adapter = _ScriptedAdapter([
        (_) => _json(404, {'code': 'doc_not_found'}),
      ]);
      final api = DocsApi(_makeDio(adapter));
      await expectLater(api.getDoc('x'), throwsA(isA<DioException>()));
    });
  });
}
