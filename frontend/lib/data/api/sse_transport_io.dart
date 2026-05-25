import 'package:dio/dio.dart';

import 'sse_client.dart';
import 'sse_transport.dart';

/// io / 移动端 / 桌面 / VM 测试：走 dio `ResponseType.stream`。
///
/// `receiveTimeout` / `sendTimeout` 显式 24h：dio 5.x BrowserHttpClientAdapter 把
/// `Duration.zero` 当作"未 override" → 退化到 BaseOptions 默认 30s → SSE 跑超过 30s
/// 触发 `DioException [receive timeout]`。本 io 实现虽不跑在 web adapter 上，但保持与
/// web 实现一致，并钉死回归测（messages_api_test / checkpoint_api_test）。
Stream<SseFrame> openSseFramesImpl(SseRequest req) async* {
  final resp = await req.dio.post<ResponseBody>(
    req.path,
    data: req.jsonBody,
    options: Options(
      responseType: ResponseType.stream,
      headers: {'Accept': 'text/event-stream'},
      receiveTimeout: const Duration(hours: 24),
      sendTimeout: const Duration(hours: 24),
    ),
    cancelToken: req.cancelToken,
  );
  yield* sseFramesFromBytes(resp.data!.stream);
}
