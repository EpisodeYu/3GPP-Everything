import 'package:tgpp/data/api/docs_api.dart';

/// 内存版 DocsApi，用于 reader / citation chip / app shell doc picker 的 widget 测。
///
/// 默认空数据；各 case 自行注入 [docs] / [sectionMap] / [chunkMap] / [searchMap]。
class FakeDocsApi implements DocsApi {
  FakeDocsApi({
    this.docs = const [],
    Map<String, DocDetailResponse>? specDetails,
    Map<String, SectionDetailResponse>? sectionMap,
    Map<String, ChunkOut>? chunkMap,
    Map<String, SearchResponse>? searchMap,
  })  : specDetails = specDetails ?? const {},
        sectionMap = sectionMap ?? const {},
        chunkMap = chunkMap ?? const {},
        searchMap = searchMap ?? const {};

  /// `GET /docs` 返回的文档列表（受 release/series 过滤）。
  final List<DocOut> docs;

  /// `GET /docs/{spec_id}` 按 specId 返回；key 缺失 → 抛 StateError。
  final Map<String, DocDetailResponse> specDetails;

  /// `GET /docs/{spec_id}/sections/{path}` 按 `"specId/sectionPath"` 返回。
  final Map<String, SectionDetailResponse> sectionMap;

  /// `GET /chunks/{chunk_id}` 按 chunkId 返回。
  final Map<String, ChunkOut> chunkMap;

  /// `GET /docs/{spec_id}/search?q=` 按 `"specId/query"` 返回。
  final Map<String, SearchResponse> searchMap;

  /// 单测自由读：上一次 search 的 (specId, query) 对。
  String? lastSearchedSpec;
  String? lastSearchedQuery;
  int searchCalls = 0;
  int listCalls = 0;

  @override
  Future<DocListResponse> list({String? release, String? series}) async {
    listCalls += 1;
    final filtered = docs.where((d) {
      if (release != null && release.isNotEmpty && d.release != release) return false;
      if (series != null && series.isNotEmpty && d.series != series) return false;
      return true;
    }).toList();
    return DocListResponse(items: filtered, total: filtered.length);
  }

  @override
  Future<DocDetailResponse> getDoc(String specId) async {
    final d = specDetails[specId];
    if (d == null) throw StateError('FakeDocsApi: no spec detail for $specId');
    return d;
  }

  @override
  Future<SectionDetailResponse> getSection(String specId, String sectionPath) async {
    final key = '$specId/$sectionPath';
    final s = sectionMap[key];
    if (s == null) throw StateError('FakeDocsApi: no section for $key');
    return s;
  }

  @override
  Future<SearchResponse> search(String specId, String q) async {
    searchCalls += 1;
    lastSearchedSpec = specId;
    lastSearchedQuery = q;
    final key = '$specId/$q';
    return searchMap[key] ??
        SearchResponse(specId: specId, query: q, items: const []);
  }

  @override
  Future<ChunkOut> getChunk(String chunkId) async {
    final c = chunkMap[chunkId];
    if (c == null) throw StateError('FakeDocsApi: no chunk for $chunkId');
    return c;
  }
}

DocOut buildDocOut({
  required String specId,
  String release = 'Rel-18',
  String series = '23',
  String title = '',
  int chunkCount = 0,
}) =>
    DocOut(
      specId: specId,
      release: release,
      series: series,
      title: title,
      chunkCount: chunkCount,
    );

DocDetailResponse buildDocDetail({
  required String specId,
  String release = 'Rel-18',
  String series = '23',
  List<SectionNode> sections = const [],
}) =>
    DocDetailResponse(
      specId: specId,
      release: release,
      series: series,
      sections: sections,
    );

SectionNode buildSectionNode({
  required String path,
  String title = '',
  int chunkCount = 1,
}) =>
    SectionNode(
      sectionPath: path.split('.'),
      sectionTitle: title,
      chunkCount: chunkCount,
    );

ChunkOut buildChunk({
  required String chunkId,
  String specId = '23.501',
  String sectionPath = '5.6.1',
  String sectionTitle = '',
  String content = '',
  String chunkType = 'text',
}) =>
    ChunkOut(
      chunkId: chunkId,
      specId: specId,
      sectionPath: sectionPath.split('.'),
      sectionTitle: sectionTitle,
      chunkType: chunkType,
      content: content,
    );

SectionDetailResponse buildSectionDetail({
  required String specId,
  required String sectionPath,
  String sectionTitle = '',
  required List<ChunkOut> chunks,
}) =>
    SectionDetailResponse(
      specId: specId,
      sectionPath: sectionPath.split('.'),
      sectionTitle: sectionTitle,
      chunks: chunks,
    );

SearchHit buildSearchHit({
  required String chunkId,
  String specId = '23.501',
  String sectionPath = '5.6',
  String sectionTitle = '',
  String preview = '',
  String chunkType = 'text',
}) =>
    SearchHit(
      chunkId: chunkId,
      specId: specId,
      sectionPath: sectionPath.split('.'),
      sectionTitle: sectionTitle,
      chunkType: chunkType,
      preview: preview,
    );
