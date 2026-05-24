import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/api/docs_api.dart';

/// `/docs/{spec_id}`：spec 章节树 + metadata。
///
/// 同一 specId 多 widget 共享缓存（toc drawer 与 section 内容会同时订阅）。
final docDetailProvider =
    FutureProvider.autoDispose.family<DocDetailResponse, String>(
  (ref, specId) async {
    return ref.watch(docsApiProvider).getDoc(specId);
  },
);

/// 单 section 详情参数（spec + section path），用作 family key。
class SectionRef {
  const SectionRef({required this.specId, required this.sectionPath});

  final String specId;
  final String sectionPath;

  @override
  bool operator ==(Object other) =>
      other is SectionRef &&
      other.specId == specId &&
      other.sectionPath == sectionPath;

  @override
  int get hashCode => Object.hash(specId, sectionPath);

  @override
  String toString() => 'SectionRef($specId/$sectionPath)';
}

/// `/docs/{spec_id}/sections/{section_path}`：单 section 的所有 chunks。
final sectionDetailProvider =
    FutureProvider.autoDispose.family<SectionDetailResponse, SectionRef>(
  (ref, sref) async {
    return ref.watch(docsApiProvider).getSection(sref.specId, sref.sectionPath);
  },
);

/// 单 spec 内搜索（toc drawer 上方搜索框）；空串 → 不查。
class SearchRef {
  const SearchRef({required this.specId, required this.query});

  final String specId;
  final String query;

  @override
  bool operator ==(Object other) =>
      other is SearchRef &&
      other.specId == specId &&
      other.query == query;

  @override
  int get hashCode => Object.hash(specId, query);
}

final docSearchProvider =
    FutureProvider.autoDispose.family<SearchResponse, SearchRef>(
  (ref, sref) async {
    if (sref.query.trim().isEmpty) {
      return SearchResponse(specId: sref.specId, query: sref.query, items: const []);
    }
    return ref.watch(docsApiProvider).search(sref.specId, sref.query.trim());
  },
);
