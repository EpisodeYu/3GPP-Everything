import 'dart:async';
import 'dart:convert';

/// 一条 SSE 帧（`event:` + 一/多行 `data:`，由空行分隔）。
///
/// `event` 缺失时默认 `message`（SSE spec）；后端 sse-starlette 每帧都会带 `event:`，
/// 但解析器仍对 spec 完整兼容（注释行 `:`、`id:` / `retry:` 字段直接忽略）。
class SseFrame {
  const SseFrame({required this.event, required this.data});

  final String event;
  final String data;

  @override
  bool operator ==(Object other) =>
      other is SseFrame && other.event == event && other.data == data;

  @override
  int get hashCode => Object.hash(event, data);

  @override
  String toString() => 'SseFrame(event: $event, data: $data)';
}

/// 行级 SSE 解析器：按 SSE spec 喂行 → 在遇到空行时 emit 一帧。
///
/// 单帧规则（节选自 W3C SSE / WhatWG）：
/// - 以 `:` 开头的行是注释，整行丢弃
/// - `event: <name>` 设置当前帧名
/// - `data: <value>` 累加到 data 缓冲；多个 `data:` 行用 `\n` 拼接
/// - 空行 → 把当前缓冲 emit 成一帧后清空状态
/// - 未识别字段（`id`, `retry` 等）直接忽略
/// - 冒号后单个空格按 spec 剥掉
///
/// 与后端 sse-starlette 行为对齐：
/// - 每 15s 发 `: ping` 注释行（防 nginx 缓冲断流），解析器原样丢弃
/// - data 永远是 JSON 字符串（chat 路由用 `json.dumps`）；解析器不替业务做 JSON 解码
class SseLineParser {
  String _eventName = 'message';
  final StringBuffer _data = StringBuffer();
  bool _hasData = false;
  bool _hasEventName = false;

  /// 喂一行，可能返回 0 或 1 个完整帧。
  ///
  /// 不应传带 `\n` 的字符串；调用者用 [LineSplitter] 之类拆好。
  SseFrame? feed(String line) {
    if (line.startsWith(':')) {
      return null;
    }
    if (line.isEmpty) {
      if (!_hasData && !_hasEventName) {
        return null;
      }
      final frame = SseFrame(event: _eventName, data: _data.toString());
      _reset();
      return frame;
    }

    final colon = line.indexOf(':');
    final String field;
    String value;
    if (colon < 0) {
      // SSE spec：没有冒号的行整行视为字段名、value 为空
      field = line;
      value = '';
    } else {
      field = line.substring(0, colon);
      value = line.substring(colon + 1);
      if (value.startsWith(' ')) {
        value = value.substring(1);
      }
    }

    switch (field) {
      case 'event':
        _eventName = value;
        _hasEventName = true;
        break;
      case 'data':
        if (_hasData) {
          _data.write('\n');
        }
        _data.write(value);
        _hasData = true;
        break;
      // `id` / `retry` 等字段 MVP 不用，按 spec 默认丢弃
    }
    return null;
  }

  void _reset() {
    _eventName = 'message';
    _data.clear();
    _hasData = false;
    _hasEventName = false;
  }
}

/// 用 [SseLineParser] 同步喂一组行；适合单测。
List<SseFrame> parseSseLines(Iterable<String> lines) {
  final parser = SseLineParser();
  final out = <SseFrame>[];
  for (final l in lines) {
    final f = parser.feed(l);
    if (f != null) out.add(f);
  }
  return out;
}

/// 把字节流转成 [SseFrame] 流。生产路径用：
///
/// ```dart
/// final resp = await dio.post<ResponseBody>(..., options: Options(responseType: ResponseType.stream));
/// await for (final f in sseFramesFromBytes(resp.data!.stream)) { ... }
/// ```
Stream<SseFrame> sseFramesFromBytes(Stream<List<int>> bytes) async* {
  final parser = SseLineParser();
  // dio 的 ResponseBody.stream 是 Stream<Uint8List>；Dart 3.x Stream.transform 在
  // 运行时按 Stream<T> 的 T 做 transformer 类型检查，而 `utf8.decoder` 是
  // StreamTransformer<List<int>, String>，不是 <Uint8List, ...>。显式
  // .cast<List<int>>() 拿到 element 类型固定为 List<int> 的包装流，避免该检查
  // 在 Stream<Uint8List> 上报 "Utf8Decoder is not a subtype of
  // StreamTransformer<Uint8List, String>"。
  await for (final line in bytes
      .cast<List<int>>()
      .transform(utf8.decoder)
      .transform(const LineSplitter())) {
    final frame = parser.feed(line);
    if (frame != null) yield frame;
  }
}
