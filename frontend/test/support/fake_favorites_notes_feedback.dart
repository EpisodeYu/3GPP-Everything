import 'package:tgpp/data/api/favorites_api.dart';
import 'package:tgpp/data/api/feedback_api.dart';
import 'package:tgpp/data/api/notes_api.dart';

/// 收藏 / 笔记 / 反馈 API 的最小内存版 fake，给 chat_page 的长按菜单测试用。

class FakeFavoritesApi implements FavoritesApi {
  FakeFavoritesApi({this.items = const []});

  /// list() 回放的种子数据（收藏页测试用）。
  List<FavoriteOut> items;

  int createCalls = 0;
  String? lastTargetType;
  String? lastTargetId;
  bool failNext = false;
  final List<String> deletedIds = [];

  @override
  Future<FavoriteOut> create({
    required String targetType,
    required String targetId,
  }) async {
    createCalls += 1;
    lastTargetType = targetType;
    lastTargetId = targetId;
    if (failNext) {
      failNext = false;
      throw const FormatException('favorites_fail');
    }
    return FavoriteOut(
      id: 'fav-$createCalls',
      targetType: targetType,
      targetId: targetId,
      createdAt: DateTime.utc(2026, 5, 24, 21, createCalls),
    );
  }

  @override
  Future<FavoriteListResponse> list({String? targetType}) async =>
      FavoriteListResponse(items: items);

  @override
  Future<void> delete(String fid) async {
    deletedIds.add(fid);
    items = items.where((f) => f.id != fid).toList();
  }
}

class FakeNotesApi implements NotesApi {
  FakeNotesApi({this.items = const []});

  /// list() 回放的种子数据（笔记页测试用）。
  List<NoteOut> items;

  int createCalls = 0;
  String? lastTargetType;
  String? lastTargetId;
  String? lastBody;
  bool failNext = false;
  final List<String> deletedIds = [];
  final Map<String, String> patched = {};

  @override
  Future<NoteOut> create({
    required String targetType,
    required String targetId,
    String body = '',
  }) async {
    createCalls += 1;
    lastTargetType = targetType;
    lastTargetId = targetId;
    lastBody = body;
    if (failNext) {
      failNext = false;
      throw const FormatException('notes_fail');
    }
    final now = DateTime.utc(2026, 5, 24, 21, createCalls);
    return NoteOut(
      id: 'note-$createCalls',
      targetType: targetType,
      targetId: targetId,
      body: body,
      createdAt: now,
      updatedAt: now,
    );
  }

  @override
  Future<NoteListResponse> list({String? targetType, String? targetId}) async =>
      NoteListResponse(items: items);

  @override
  Future<NoteOut> patch(String nid, {required String body}) async {
    patched[nid] = body;
    final now = DateTime.utc(2026, 5, 24, 22);
    items = [
      for (final n in items)
        if (n.id == nid)
          NoteOut(
            id: n.id,
            targetType: n.targetType,
            targetId: n.targetId,
            body: body,
            createdAt: n.createdAt,
            updatedAt: now,
            sessionId: n.sessionId,
            preview: n.preview,
          )
        else
          n,
    ];
    return NoteOut(
      id: nid,
      targetType: 'message',
      targetId: 'target',
      body: body,
      createdAt: now,
      updatedAt: now,
    );
  }

  @override
  Future<void> delete(String nid) async {
    deletedIds.add(nid);
    items = items.where((n) => n.id != nid).toList();
  }
}

class FakeFeedbackApi implements FeedbackApi {
  int upsertCalls = 0;
  String? lastMessageId;
  int? lastThumb;
  String? lastReason;
  bool failNext = false;

  @override
  Future<FeedbackOut> upsert(
    String messageId, {
    required int thumb,
    String? reason,
  }) async {
    upsertCalls += 1;
    lastMessageId = messageId;
    lastThumb = thumb;
    lastReason = reason;
    if (failNext) {
      failNext = false;
      throw const FormatException('feedback_fail');
    }
    return FeedbackOut(
      id: 'fb-$upsertCalls',
      messageId: messageId,
      thumb: thumb,
      reason: reason,
      createdAt: DateTime.utc(2026, 5, 24, 21, upsertCalls),
    );
  }
}
