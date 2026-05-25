import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';
import 'sse_client.dart';

/// POST `/sessions/{sid}/messages` body。
class SendMessageBody {
  const SendMessageBody({
    required this.content,
    this.mode,
    this.explicitTools = const [],
  });

  final String content;

  /// `'qa'` | `'raw_lookup'`；null → 用会话 default。
  final String? mode;

  /// 显式工具勾选；空 → Agent 不调任何工具。
  final List<String> explicitTools;

  Map<String, dynamic> toJson() => {
        'content': content,
        if (mode != null) 'mode': mode,
        'explicit_tools': explicitTools,
      };
}

/// 后端 10 类 SSE 事件的强类型表示。
///
/// 协议锚点：`backend/app/api/v1/chat.py` 头注释 +
/// `docs/03-development/05-frontend.md §8`。
///
/// 设计取舍：
/// - 用 sealed-style 抽象 + 子类 `is` 分发；不引 freezed，10 类事件手写够用。
/// - 不强结构化未知字段（`summary` / `citations`）：直接保留 `Map<String, dynamic>`，
///   留给上层（`ChatController` / UI）按需读取，避免后端加字段就要改 model。
sealed class ChatEvent {
  const ChatEvent();

  /// 解析单帧 SSE → 类型化事件；未知 event 名 → [UnknownChatEvent]。
  static ChatEvent fromFrame(SseFrame frame) {
    final raw = frame.data;
    final data = raw.isEmpty ? const <String, dynamic>{} : jsonDecode(raw);
    if (data is! Map<String, dynamic>) {
      return UnknownChatEvent(name: frame.event, data: const {});
    }
    switch (frame.event) {
      case 'run_start':
        return RunStartEvent(
          runId: data['run_id'] as String,
          sessionId: data['session_id'] as String,
          messageId: data['message_id'] as String,
        );
      case 'node_start':
        return NodeStartEvent(node: data['node'] as String);
      case 'node_end':
        return NodeEndEvent(
          node: data['node'] as String,
          durationMs: (data['duration_ms'] as num?)?.toInt() ?? 0,
          summary: (data['summary'] as Map?)?.cast<String, dynamic>() ?? const {},
        );
      case 'chunks_hit':
      case 'chunks_rerank':
        // payload: {"type": "chunks_hit" | "chunks_rerank", "chunks": [...]}
        final chunks = (data['chunks'] as List?)
                ?.cast<Map<String, dynamic>>()
                .map(ChunkPreview.fromJson)
                .toList() ??
            const <ChunkPreview>[];
        return frame.event == 'chunks_hit'
            ? ChunksHitEvent(chunks: chunks)
            : ChunksRerankEvent(chunks: chunks);
      case 'token':
        return TokenEvent(delta: (data['delta'] as String?) ?? '');
      case 'final':
        return FinalEvent(
          messageId: data['message_id'] as String,
          answer: (data['answer'] as String?) ?? '',
          citations: (data['citations'] as List?)
                  ?.cast<Map<String, dynamic>>()
                  .map(Citation.fromJson)
                  .toList() ??
              const <Citation>[],
          confidence: (data['confidence'] as num?)?.toDouble() ?? 0.0,
        );
      case 'cancelled':
        return CancelledEvent(reason: (data['reason'] as String?) ?? '');
      case 'error':
        return ErrorEvent(
          code: (data['code'] as String?) ?? 'unknown',
          message: (data['message'] as String?) ?? '',
        );
      case 'end':
        return const EndEvent();
      default:
        return UnknownChatEvent(name: frame.event, data: data);
    }
  }
}

class RunStartEvent extends ChatEvent {
  const RunStartEvent({
    required this.runId,
    required this.sessionId,
    required this.messageId,
  });
  final String runId;
  final String sessionId;
  final String messageId;
}

class NodeStartEvent extends ChatEvent {
  const NodeStartEvent({required this.node});
  final String node;
}

class NodeEndEvent extends ChatEvent {
  const NodeEndEvent({
    required this.node,
    required this.durationMs,
    required this.summary,
  });
  final String node;
  final int durationMs;
  final Map<String, dynamic> summary;
}

class ChunkPreview {
  const ChunkPreview({
    required this.chunkId,
    required this.specId,
    required this.sectionPath,
    this.sectionTitle,
    this.score,
    this.rerankScore,
    this.preview = '',
  });

  factory ChunkPreview.fromJson(Map<String, dynamic> j) => ChunkPreview(
        chunkId: (j['chunk_id'] as String?) ?? '',
        specId: (j['spec_id'] as String?) ?? '',
        sectionPath: (j['section_path'] as String?) ?? '',
        sectionTitle: j['section_title'] as String?,
        score: (j['score'] as num?)?.toDouble(),
        rerankScore: (j['rerank_score'] as num?)?.toDouble(),
        preview: (j['preview'] as String?) ?? '',
      );

  final String chunkId;
  final String specId;
  final String sectionPath;
  final String? sectionTitle;
  final double? score;
  final double? rerankScore;
  final String preview;
}

class ChunksHitEvent extends ChatEvent {
  const ChunksHitEvent({required this.chunks});
  final List<ChunkPreview> chunks;
}

class ChunksRerankEvent extends ChatEvent {
  const ChunksRerankEvent({required this.chunks});
  final List<ChunkPreview> chunks;
}

class TokenEvent extends ChatEvent {
  const TokenEvent({required this.delta});
  final String delta;
}

class Citation {
  const Citation({
    required this.chunkId,
    required this.specId,
    required this.sectionPath,
    this.sectionTitle,
    this.citeSectionPath,
    this.rerankScore,
  });

  factory Citation.fromJson(Map<String, dynamic> j) => Citation(
        chunkId: (j['chunk_id'] as String?) ?? '',
        specId: (j['spec_id'] as String?) ?? '',
        sectionPath: (j['section_path'] as String?) ?? '',
        sectionTitle: j['section_title'] as String?,
        citeSectionPath: j['cite_section_path'] as String?,
        rerankScore: (j['rerank_score'] as num?)?.toDouble(),
      );

  final String chunkId;
  final String specId;
  final String sectionPath;
  final String? sectionTitle;
  final String? citeSectionPath;
  final double? rerankScore;
}

class FinalEvent extends ChatEvent {
  const FinalEvent({
    required this.messageId,
    required this.answer,
    required this.citations,
    required this.confidence,
  });
  final String messageId;
  final String answer;
  final List<Citation> citations;
  final double confidence;
}

class CancelledEvent extends ChatEvent {
  const CancelledEvent({required this.reason});
  final String reason;
}

class ErrorEvent extends ChatEvent {
  const ErrorEvent({required this.code, required this.message});
  final String code;
  final String message;
}

class EndEvent extends ChatEvent {
  const EndEvent();
}

/// 后端日后加新 event name 时降级处理；UI 忽略它就行。
class UnknownChatEvent extends ChatEvent {
  const UnknownChatEvent({required this.name, required this.data});
  final String name;
  final Map<String, dynamic> data;
}

/// 与后端 `MessageCitationOut`（详见 `backend/app/schemas/messages.py`）对齐。
class MessageCitationOut {
  const MessageCitationOut({
    required this.chunkId,
    required this.rank,
    required this.specId,
    required this.sectionPath,
    this.rerankScore,
    this.charOffsetStart,
    this.charOffsetEnd,
  });

  factory MessageCitationOut.fromJson(Map<String, dynamic> j) => MessageCitationOut(
        chunkId: (j['chunk_id'] as String?) ?? '',
        rank: (j['rank'] as num?)?.toInt() ?? 0,
        specId: (j['spec_id'] as String?) ?? '',
        sectionPath: (j['section_path'] as String?) ?? '',
        rerankScore: (j['rerank_score'] as num?)?.toDouble(),
        charOffsetStart: (j['char_offset_start'] as num?)?.toInt(),
        charOffsetEnd: (j['char_offset_end'] as num?)?.toInt(),
      );

  final String chunkId;
  final int rank;
  final String specId;
  final String sectionPath;
  final double? rerankScore;

  /// 后端 `MessageCitationOut.char_offset_start/end`：本期 reader 未消费，
  /// 留字段以满足 schema 漂移 CI 校验，M7+ 把"句子级高亮"做起来时再消费。
  final int? charOffsetStart;
  final int? charOffsetEnd;
}

/// 与后端 `MessageOut` 对齐（`backend/app/schemas/messages.py`）。
class MessageOut {
  const MessageOut({
    required this.id,
    required this.sessionId,
    required this.role,
    required this.content,
    required this.status,
    required this.createdAt,
    this.mode,
    this.explicitTools = const [],
    this.confidence,
    this.selfRagVerdict,
    this.langgraphRunId,
    this.citations = const [],
  });

  factory MessageOut.fromJson(Map<String, dynamic> j) => MessageOut(
        id: j['id'] as String,
        sessionId: j['session_id'] as String,
        role: j['role'] as String,
        content: (j['content'] as String?) ?? '',
        status: j['status'] as String,
        mode: j['mode'] as String?,
        explicitTools: ((j['explicit_tools'] as List?) ?? const [])
            .cast<String>(),
        confidence: (j['confidence'] as num?)?.toDouble(),
        selfRagVerdict: j['self_rag_verdict'] as String?,
        langgraphRunId: j['langgraph_run_id'] as String?,
        createdAt: DateTime.parse(j['created_at'] as String),
        citations: ((j['citations'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(MessageCitationOut.fromJson)
            .toList(),
      );

  final String id;
  final String sessionId;
  final String role; // 'user' | 'assistant' | 'system'
  final String content;
  final String status; // 'ok' | 'cancelled' | 'failed'
  final String? mode;
  final List<String> explicitTools;
  final double? confidence;
  final String? selfRagVerdict;
  final String? langgraphRunId;
  final DateTime createdAt;
  final List<MessageCitationOut> citations;
}

class MessageListResponse {
  const MessageListResponse({required this.items, required this.total});

  factory MessageListResponse.fromJson(Map<String, dynamic> j) =>
      MessageListResponse(
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(MessageOut.fromJson)
            .toList(),
        total: (j['total'] as num).toInt(),
      );

  final List<MessageOut> items;
  final int total;
}

class MessagesApi {
  MessagesApi(this._dio);

  final Dio _dio;

  /// GET `/sessions/{sid}/messages`：拉历史消息（含 citations）。
  Future<MessageListResponse> list(
    String sid, {
    int page = 1,
    int pageSize = 200,
  }) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/sessions/$sid/messages',
      queryParameters: {'page': page, 'page_size': pageSize},
    );
    return MessageListResponse.fromJson(resp.data!);
  }

  /// 发消息并返回 SSE 事件流。
  ///
  /// 调用者必须 listen 该流；当需要终止 HTTP 连接时（用户点取消按钮 / 切会话），
  /// 调 [CancelToken.cancel] —— dio 会终止 underlying 流，async generator 退出。
  /// 不传 [cancelToken] 也能跑，但失去取消能力。
  Stream<ChatEvent> sendMessage(
    String sid,
    SendMessageBody body, {
    CancelToken? cancelToken,
  }) async* {
    final resp = await _dio.post<ResponseBody>(
      '/sessions/$sid/messages',
      data: body.toJson(),
      options: Options(
        responseType: ResponseType.stream,
        headers: {'Accept': 'text/event-stream'},
        // SSE 一次 RAG 通常 20–60s，全量 generate 偶尔过 1 min。
        // dio web 的 BrowserHttpClientAdapter 把 `Duration.zero` 当作"未 override"
        // 处理 → 退化到 BaseOptions.receiveTimeout=30s 触发 receive timeout。
        // 显式给一个超长有限值（24h）绕过该 web adapter 行为；io 端同样宽容。
        receiveTimeout: const Duration(hours: 24),
        sendTimeout: const Duration(hours: 24),
      ),
      cancelToken: cancelToken,
    );
    await for (final frame in sseFramesFromBytes(resp.data!.stream)) {
      yield ChatEvent.fromFrame(frame);
    }
  }

  /// DELETE `/sessions/{sid}/runs/{rid}` —— 204 表示后端已收到取消请求。
  /// 是否真的中断要看后续 SSE 流是否吐 cancelled / final。
  Future<void> cancelRun(String sid, String runId) async {
    await _dio.delete<void>('/sessions/$sid/runs/$runId');
  }
}

final messagesApiProvider =
    Provider<MessagesApi>((ref) => MessagesApi(ref.watch(dioProvider)));
