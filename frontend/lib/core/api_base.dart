/// API 入口地址。
///
/// 通过编译期 `--dart-define=API_BASE_URL=...` 注入。
/// dev 默认指向同机后端；生产建议同源 nginx 反代后由 nginx 提供，
/// 此时也可在构建脚本里覆盖为 `/api/v1`。
class ApiBase {
  ApiBase._();

  static const String url = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://localhost:8002/api/v1',
  );
}
