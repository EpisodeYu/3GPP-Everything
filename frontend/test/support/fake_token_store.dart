import 'package:tgpp/data/storage/token_store.dart';

class FakeTokenStore implements TokenStore {
  FakeTokenStore({this.access, this.refresh});

  String? access;
  String? refresh;
  int clearCalls = 0;
  int writeCalls = 0;

  @override
  Future<String?> readAccess() async => access;

  @override
  Future<String?> readRefresh() async => refresh;

  @override
  Future<void> write({required String access, required String refresh}) async {
    this.access = access;
    this.refresh = refresh;
    writeCalls += 1;
  }

  @override
  Future<void> clear() async {
    access = null;
    refresh = null;
    clearCalls += 1;
  }
}
