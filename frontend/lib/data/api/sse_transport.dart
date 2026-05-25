import 'package:dio/dio.dart';

import 'sse_client.dart';
import 'sse_transport_io.dart'
    if (dart.library.js_interop) 'sse_transport_web.dart';

/// 一次 SSE 请求所需的全部参数；io 与 web 两套实现共用此结构。
///
/// - **io / 移动端 / 桌面 / `flutter test`（VM）**：用 [dio] 走 `ResponseType.stream`，
///   保留 dio_provider 的 AuthInterceptor（token 注入 + 401 刷新）。
/// - **web**：dio 5.x 的 BrowserHttpClientAdapter（XHR）会把整段响应缓冲到结束才交付，
///   `ResponseType.stream` 在 web 上退化成"一次性整包"——RAG/推理/answer 的 token 无法
///   逐字到达。web 实现改用浏览器 Fetch API + ReadableStream 真流式；因绕过了 dio
///   interceptor，token / 401 刷新需手动带，故额外要 [baseUrl] /
///   [readAccessToken] / [refreshAccessToken] / [onAuthLost]。
class SseRequest {
  const SseRequest({
    required this.dio,
    required this.baseUrl,
    required this.path,
    this.jsonBody,
    this.cancelToken,
    required this.readAccessToken,
    required this.refreshAccessToken,
    this.onAuthLost,
  });

  /// io 路径直接发请求；web 路径只借它的 [CancelToken] 桥接到 AbortController。
  final Dio dio;

  /// 绝对 base（`ApiBase.url`）；web 拼完整 URL 用。io 由 `dio.baseUrl` 兜底。
  final String baseUrl;

  /// 相对路径，如 `/sessions/$sid/messages`。
  final String path;

  /// POST JSON 请求体；resume 等无 body 路由传 null。
  final Map<String, dynamic>? jsonBody;

  final CancelToken? cancelToken;

  /// 取当前 access token（web 路径用）。
  final Future<String?> Function() readAccessToken;

  /// 刷新 access token（web 路径 401 时调一次，返回新 token 或 null）。
  final Future<String?> Function() refreshAccessToken;

  /// 刷新失败（web 路径）回调，复用 dio_provider 的登出逻辑。
  final void Function()? onAuthLost;
}

/// 打开一条 SSE 帧流。平台实现由条件 import 选择（`dart.library.js_interop`→web）。
Stream<SseFrame> openSseFrames(SseRequest req) => openSseFramesImpl(req);
