import 'dart:convert';
import 'dart:js_interop';

import 'package:web/web.dart' as web;

import 'sse_client.dart';
import 'sse_transport.dart';

/// Web 真流式 SSE：用浏览器 Fetch API + ReadableStream。
///
/// 为什么不用 dio：dio 5.x 的 BrowserHttpClientAdapter（XHR）把整段响应缓冲到结束
/// 才交付，`ResponseType.stream` 在 web 上退化成"一次性整包"——RAG/推理/answer 的
/// token 无法逐字到达。Fetch 的 `response.body` 是 ReadableStream，可逐 chunk 读取，
/// 配合既有 [sseFramesFromBytes] 解析即得到真流式。
///
/// auth：绕过了 dio AuthInterceptor，手动带 `Authorization: Bearer`；401 时调一次
/// [SseRequest.refreshAccessToken] 后重试，仍失败则 [SseRequest.onAuthLost] + 抛错。
/// cancel：把 dio [CancelToken] 桥接到 Fetch 的 AbortController。
Stream<SseFrame> openSseFramesImpl(SseRequest req) {
  final controller = web.AbortController();
  // dio CancelToken 取消 → abort fetch。未取消时该 future 永不完成，无副作用。
  req.cancelToken?.whenCancel.then((_) {
    if (!controller.signal.aborted) controller.abort();
  });
  return _stream(req, controller);
}

Stream<SseFrame> _stream(SseRequest req, web.AbortController controller) async* {
  Future<web.Response> doFetch(String token) {
    final headers = web.Headers()..set('Accept', 'text/event-stream');
    if (req.jsonBody != null) headers.set('Content-Type', 'application/json');
    if (token.isNotEmpty) headers.set('Authorization', 'Bearer $token');
    final init = web.RequestInit(
      method: 'POST',
      headers: headers,
      signal: controller.signal,
      body: req.jsonBody == null ? null : jsonEncode(req.jsonBody).toJS,
    );
    return web.window.fetch((req.baseUrl + req.path).toJS, init).toDart;
  }

  final token = (await req.readAccessToken()) ?? '';
  var resp = await doFetch(token);
  if (resp.status == 401) {
    final fresh = await req.refreshAccessToken();
    if (fresh == null || fresh.isEmpty) {
      req.onAuthLost?.call();
      throw _SseHttpException(401);
    }
    resp = await doFetch(fresh);
  }
  if (resp.status < 200 || resp.status >= 300) {
    throw _SseHttpException(resp.status);
  }
  final body = resp.body;
  if (body == null) return;

  final reader = web.ReadableStreamDefaultReader(body);
  try {
    yield* sseFramesFromBytes(_readChunks(reader));
  } finally {
    // 提前结束（消费方取消订阅 / 异常）：abort 底层连接，释放挂起的连接。
    if (!controller.signal.aborted) controller.abort();
  }
}

/// 把 ReadableStream 的字节 chunk 转成 `Stream<List<int>>` 喂给 [sseFramesFromBytes]。
Stream<List<int>> _readChunks(web.ReadableStreamDefaultReader reader) async* {
  while (true) {
    web.ReadableStreamReadResult result;
    try {
      result = await reader.read().toDart;
    } catch (_) {
      // abort 会让挂起的 read() reject（AbortError）；视作流正常结束。
      break;
    }
    if (result.done) break;
    final value = result.value;
    if (value != null) {
      yield (value as JSUint8Array).toDart;
    }
  }
}

class _SseHttpException implements Exception {
  _SseHttpException(this.status);
  final int status;
  @override
  String toString() => 'sse_http_$status';
}
