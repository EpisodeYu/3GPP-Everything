import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:tgpp/data/api/sse_client.dart';

void main() {
  group('SseLineParser', () {
    test('忽略 `:` 开头的注释行（包含 sse-starlette `: ping`）', () {
      final frames = parseSseLines([': ping', '']);
      expect(frames, isEmpty,
          reason: '只有注释行 + 空行不应 emit 任何完整帧');
    });

    test('event: + data: 单行 → 一帧', () {
      final frames = parseSseLines([
        'event: token',
        'data: {"delta":"hi"}',
        '',
      ]);
      expect(frames.length, 1);
      expect(frames.first.event, 'token');
      expect(frames.first.data, '{"delta":"hi"}');
    });

    test('多行 data: 用 \\n 拼接', () {
      final frames = parseSseLines([
        'event: final',
        'data: line-1',
        'data: line-2',
        'data: line-3',
        '',
      ]);
      expect(frames.length, 1);
      expect(frames.first.data, 'line-1\nline-2\nline-3');
    });

    test('空行作为帧边界，连续两帧正确分隔', () {
      final frames = parseSseLines([
        'event: node_start',
        'data: {"node":"retrieve"}',
        '',
        'event: node_end',
        'data: {"node":"retrieve","duration_ms":80}',
        '',
      ]);
      expect(frames.length, 2);
      expect(frames[0].event, 'node_start');
      expect(frames[1].event, 'node_end');
    });

    test('注释行夹在两条 data 之间被静默丢弃', () {
      final frames = parseSseLines([
        'event: token',
        'data: a',
        ': ping mid-stream',
        'data: b',
        '',
      ]);
      expect(frames.length, 1);
      expect(frames.first.data, 'a\nb');
    });

    test('缺 event: 走 spec 默认 "message"', () {
      final frames = parseSseLines([
        'data: hello',
        '',
      ]);
      expect(frames.length, 1);
      expect(frames.first.event, 'message');
      expect(frames.first.data, 'hello');
    });

    test('冒号后单空格按 spec 剥掉，剩下的原样保留', () {
      final frames = parseSseLines([
        'event:  token', // 真冒号后留 2 个空格
        'data: {"x":1}',
        '',
      ]);
      expect(frames.length, 1);
      // 剥单空格后 'event' 字段值是 ' token'（开头有 1 个剩余空格）。
      // 这是 spec 行为；我们的 ChatEvent.fromFrame 不会匹配，但解析器忠实保留。
      expect(frames.first.event, ' token');
    });
  });

  group('sseFramesFromBytes (跨 chunk 边界)', () {
    test('被任意切片的字节流仍能聚合出完整帧', () async {
      const sse = 'event: token\ndata: a\n\nevent: token\ndata: b\n\n';
      // 把字节流故意切成小块，每块 5 字节，确保跨行 / 跨字段边界
      final bytes = utf8.encode(sse);
      Stream<List<int>> chunks() async* {
        for (var i = 0; i < bytes.length; i += 5) {
          yield bytes.sublist(i, i + 5 > bytes.length ? bytes.length : i + 5);
          // 模拟网络抖动
          await Future<void>.delayed(Duration.zero);
        }
      }

      final frames = <SseFrame>[];
      await for (final f in sseFramesFromBytes(chunks())) {
        frames.add(f);
      }
      expect(frames.length, 2);
      expect(frames[0].data, 'a');
      expect(frames[1].data, 'b');
    });

    test('注释行不会污染下一帧', () async {
      const sse = ': ping\n: another comment\nevent: end\ndata: {}\n\n';
      final bytes = utf8.encode(sse);
      Stream<List<int>> single() async* {
        yield bytes;
      }

      final frames = await sseFramesFromBytes(single()).toList();
      expect(frames.length, 1);
      expect(frames.first.event, 'end');
      expect(frames.first.data, '{}');
    });
  });
}
