/// Langfuse 控制台地址。
///
/// 通过编译期 `--dart-define=LANGFUSE_URL=https://...` 注入；默认指向官方云服务。
/// 自托管 Langfuse 部署时构建命令里覆盖一下，例如：
/// `flutter build web --dart-define=LANGFUSE_URL=https://langfuse.internal`。
class LangfuseUrl {
  LangfuseUrl._();

  static const String url = String.fromEnvironment(
    'LANGFUSE_URL',
    defaultValue: 'https://cloud.langfuse.com',
  );
}
