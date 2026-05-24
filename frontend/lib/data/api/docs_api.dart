import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'dio_provider.dart';

/// 与后端 `DocOut`（`backend/app/schemas/docs.py`）对齐。
class DocOut {
  const DocOut({
    required this.specId,
    required this.release,
    required this.series,
    this.title = '',
    this.chunkCount = 0,
  });

  factory DocOut.fromJson(Map<String, dynamic> j) => DocOut(
        specId: (j['spec_id'] as String?) ?? '',
        release: (j['release'] as String?) ?? '',
        series: (j['series'] as String?) ?? '',
        title: (j['title'] as String?) ?? '',
        chunkCount: (j['chunk_count'] as num?)?.toInt() ?? 0,
      );

  final String specId;
  final String release;
  final String series;
  final String title;
  final int chunkCount;
}

class DocListResponse {
  const DocListResponse({required this.items, required this.total});

  factory DocListResponse.fromJson(Map<String, dynamic> j) => DocListResponse(
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(DocOut.fromJson)
            .toList(),
        total: (j['total'] as num?)?.toInt() ?? 0,
      );

  final List<DocOut> items;
  final int total;
}

/// 章节树节点。与 `SectionNode` 对齐。
class SectionNode {
  const SectionNode({
    required this.sectionPath,
    required this.sectionTitle,
    required this.chunkCount,
  });

  factory SectionNode.fromJson(Map<String, dynamic> j) => SectionNode(
        sectionPath:
            ((j['section_path'] as List?) ?? const []).map((e) => e.toString()).toList(),
        sectionTitle: (j['section_title'] as String?) ?? '',
        chunkCount: (j['chunk_count'] as num?)?.toInt() ?? 0,
      );

  final List<String> sectionPath;
  final String sectionTitle;
  final int chunkCount;

  /// 点分形式，如 `5.6.1.2`；空 → `''`。
  String get joinedPath => sectionPath.join('.');
}

class DocDetailResponse {
  const DocDetailResponse({
    required this.specId,
    required this.release,
    required this.series,
    required this.sections,
  });

  factory DocDetailResponse.fromJson(Map<String, dynamic> j) => DocDetailResponse(
        specId: (j['spec_id'] as String?) ?? '',
        release: (j['release'] as String?) ?? '',
        series: (j['series'] as String?) ?? '',
        sections: ((j['sections'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(SectionNode.fromJson)
            .toList(),
      );

  final String specId;
  final String release;
  final String series;
  final List<SectionNode> sections;
}

/// 单个 chunk；与 `ChunkOut` 对齐。
class ChunkOut {
  const ChunkOut({
    required this.chunkId,
    required this.specId,
    required this.sectionPath,
    required this.sectionTitle,
    required this.chunkType,
    required this.content,
    this.charOffsetStart,
    this.charOffsetEnd,
    this.rawExtra = const {},
  });

  factory ChunkOut.fromJson(Map<String, dynamic> j) => ChunkOut(
        chunkId: (j['chunk_id'] as String?) ?? '',
        specId: (j['spec_id'] as String?) ?? '',
        sectionPath: ((j['section_path'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(),
        sectionTitle: (j['section_title'] as String?) ?? '',
        chunkType: (j['chunk_type'] as String?) ?? 'text',
        content: (j['content'] as String?) ?? '',
        charOffsetStart: (j['char_offset_start'] as num?)?.toInt(),
        charOffsetEnd: (j['char_offset_end'] as num?)?.toInt(),
        rawExtra: (j['raw_extra'] is Map)
            ? Map<String, dynamic>.from(j['raw_extra'] as Map)
            : const {},
      );

  final String chunkId;
  final String specId;
  final List<String> sectionPath;
  final String sectionTitle;
  final String chunkType;
  final String content;
  final int? charOffsetStart;
  final int? charOffsetEnd;
  final Map<String, dynamic> rawExtra;

  String get joinedPath => sectionPath.join('.');
}

class SectionDetailResponse {
  const SectionDetailResponse({
    required this.specId,
    required this.sectionPath,
    required this.sectionTitle,
    required this.chunks,
  });

  factory SectionDetailResponse.fromJson(Map<String, dynamic> j) => SectionDetailResponse(
        specId: (j['spec_id'] as String?) ?? '',
        sectionPath: ((j['section_path'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(),
        sectionTitle: (j['section_title'] as String?) ?? '',
        chunks: ((j['chunks'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(ChunkOut.fromJson)
            .toList(),
      );

  final String specId;
  final List<String> sectionPath;
  final String sectionTitle;
  final List<ChunkOut> chunks;

  String get joinedPath => sectionPath.join('.');
}

class SearchHit {
  const SearchHit({
    required this.chunkId,
    required this.specId,
    required this.sectionPath,
    required this.sectionTitle,
    required this.chunkType,
    required this.preview,
  });

  factory SearchHit.fromJson(Map<String, dynamic> j) => SearchHit(
        chunkId: (j['chunk_id'] as String?) ?? '',
        specId: (j['spec_id'] as String?) ?? '',
        sectionPath: ((j['section_path'] as List?) ?? const [])
            .map((e) => e.toString())
            .toList(),
        sectionTitle: (j['section_title'] as String?) ?? '',
        chunkType: (j['chunk_type'] as String?) ?? 'text',
        preview: (j['preview'] as String?) ?? '',
      );

  final String chunkId;
  final String specId;
  final List<String> sectionPath;
  final String sectionTitle;
  final String chunkType;
  final String preview;

  String get joinedPath => sectionPath.join('.');
}

class SearchResponse {
  const SearchResponse({
    required this.specId,
    required this.query,
    required this.items,
  });

  factory SearchResponse.fromJson(Map<String, dynamic> j) => SearchResponse(
        specId: (j['spec_id'] as String?) ?? '',
        query: (j['query'] as String?) ?? '',
        items: ((j['items'] as List?) ?? const [])
            .cast<Map<String, dynamic>>()
            .map(SearchHit.fromJson)
            .toList(),
      );

  final String specId;
  final String query;
  final List<SearchHit> items;
}

/// `/docs` Reader 路由的薄包装。
///
/// 协议锚：[`backend/app/api/v1/docs.py`](../../../../../backend/app/api/v1/docs.py) +
/// `docs/03-development/04-backend-api.md` §2 路由总表 Reader 节。
class DocsApi {
  DocsApi(this._dio);

  final Dio _dio;

  /// GET `/docs`：按 release/series 列已索引文档。M5.3 给 doc picker 用。
  Future<DocListResponse> list({String? release, String? series}) async {
    final qp = <String, dynamic>{};
    if (release != null && release.isNotEmpty) qp['release'] = release;
    if (series != null && series.isNotEmpty) qp['series'] = series;
    final resp = await _dio.get<Map<String, dynamic>>(
      '/docs',
      queryParameters: qp.isEmpty ? null : qp,
    );
    return DocListResponse.fromJson(resp.data!);
  }

  /// GET `/docs/{spec_id}`：spec 章节树。
  Future<DocDetailResponse> getDoc(String specId) async {
    final resp = await _dio.get<Map<String, dynamic>>('/docs/$specId');
    return DocDetailResponse.fromJson(resp.data!);
  }

  /// GET `/docs/{spec_id}/sections/{path}`：单 section 的所有 chunks。
  /// [sectionPath] 既支持 `"5.6.1.2"` 也支持 `"5/6/1/2"`，后端会做归一化。
  Future<SectionDetailResponse> getSection(
    String specId,
    String sectionPath,
  ) async {
    final cleaned = sectionPath.replaceAll('/', '.').replaceAll(RegExp(r'^\.+|\.+$'), '');
    final resp = await _dio.get<Map<String, dynamic>>(
      '/docs/$specId/sections/$cleaned',
    );
    return SectionDetailResponse.fromJson(resp.data!);
  }

  /// GET `/docs/{spec_id}/search`：spec 内 ILIKE 搜索。
  Future<SearchResponse> search(String specId, String q) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/docs/$specId/search',
      queryParameters: {'q': q},
    );
    return SearchResponse.fromJson(resp.data!);
  }

  /// GET `/chunks/{chunk_id}`：单 chunk 详情（bottom sheet 用）。
  Future<ChunkOut> getChunk(String chunkId) async {
    final resp = await _dio.get<Map<String, dynamic>>('/chunks/$chunkId');
    return ChunkOut.fromJson(resp.data!);
  }
}

final docsApiProvider = Provider<DocsApi>((ref) => DocsApi(ref.watch(dioProvider)));
